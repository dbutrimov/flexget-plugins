# -*- coding: utf-8 -*-
from __future__ import unicode_literals, division, absolute_import
from builtins import *  # pylint: disable=unused-import, redefined-builtin

import json
import logging
import re
from datetime import datetime, timedelta
from time import sleep
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from flexget import plugin
from flexget.db_schema import versioned_base
from flexget.entry import Entry
from flexget.event import event
from flexget.manager import Session
from flexget.plugin import PluginError
from flexget.utils import requests
from requests.auth import AuthBase
from sqlalchemy import Column, Unicode, Integer, DateTime, UniqueConstraint, ForeignKey, func
from sqlalchemy.types import TypeDecorator, VARCHAR

PLUGIN_NAME = 'lostfilm'
SCHEMA_VER = 0

log = logging.getLogger(PLUGIN_NAME)
Base = versioned_base(PLUGIN_NAME, SCHEMA_VER)


def process_url(url, base_url):
    return urljoin(base_url, url)


# region LostFilmAuthPlugin
class JSONEncodedDict(TypeDecorator):
    """
    Represents an immutable structure as a json-encoded string.

    Usage:
        JSONEncodedDict(255)
    """

    impl = VARCHAR

    def process_bind_param(self, value, dialect):
        if value is not None:
            value = json.dumps(value)
        return value

    def process_result_value(self, value, dialect):
        if value is not None:
            value = json.loads(value)
        return value


class LostFilmAccount(Base):
    __tablename__ = 'lostfilm_accounts'
    id = Column(Integer, primary_key=True, autoincrement=True, nullable=False)
    username = Column(Unicode, index=True, nullable=False, unique=True)
    cookies = Column(JSONEncodedDict)
    expiry_time = Column(DateTime, nullable=False)


class LostFilmAuth(AuthBase):
    """Supports downloading of torrents from 'lostfilm' tracker
           if you pass cookies (CookieJar) to constructor then authentication will be bypassed and cookies will be just set
        """

    def try_authenticate(self, payload):
        for _ in range(5):
            session = requests.Session()
            session.headers['User-Agent'] = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_12_1) ' \
                                            'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/54.0.2840.98 Safari/537.36'

            response = session.post(
                'http://login1.bogi.ru/login.php?referer=http%3A%2F%2Fwww.lostfilm.tv%2F',
                data=payload)
            response_html = response.text

            response_tree = BeautifulSoup(response_html, 'html.parser')
            form_node = response_tree.find('form', id='b_form')
            if form_node:
                action_url = form_node.get('action')

                action_payload = {}
                input_nodes = form_node.find_all('input', type='hidden')
                for input_node in input_nodes:
                    input_name = input_node.get('name')
                    input_value = input_node.get('value')
                    action_payload[input_name] = input_value

                session.post(action_url, data=action_payload)

                cookies = session.cookies.get_dict(domain='.lostfilm.tv')
                if cookies and len(cookies) > 0:
                    user_id = cookies.get('uid')
                    user_pass = cookies.get('pass')
                    if user_id and user_pass:
                        cookies = {'uid': user_id, 'pass': user_pass}
                        return cookies

            sleep(3)

        raise PluginError('Unable to obtain cookies from LostFilm. Looks like invalid username or password.')

    def __init__(self, username, password, cookies=None, db_session=None):
        if cookies is None:
            log.debug('LostFilm cookie not found. Requesting new one.')

            payload_ = {
                'login': username,
                'password': password,
                'module': 1,
                'target': 'http://lostfilm.tv/',
                'repage': 'user',
                'act': 'login'
            }

            self.cookies_ = self.try_authenticate(payload_)
            if db_session:
                db_session.add(
                    LostFilmAccount(
                        username=username,
                        cookies=self.cookies_,
                        expiry_time=datetime.now() + timedelta(days=1)))
                db_session.commit()
                # else:
                #     raise ValueError(
                #         'db_session can not be None if cookies is None')
        else:
            log.debug('Using previously saved cookie.')
            self.cookies_ = cookies

    def __call__(self, request):
        request.prepare_cookies(self.cookies_)
        return request


class LostFilmAuthPlugin(object):
    """Usage:

    lostfilm_auth:
      username: 'username_here'
      password: 'password_here'
    """

    schema = {
        'type': 'object',
        'properties': {
            'username': {'type': 'string'},
            'password': {'type': 'string'}
        },
        'additionalProperties': False
    }

    auth_cache = {}

    def try_find_cookie(self, db_session, username):
        account = db_session.query(LostFilmAccount).filter(LostFilmAccount.username == username).first()
        if account:
            if account.expiry_time < datetime.now():
                db_session.delete(account)
                db_session.commit()
                return None
            return account.cookies
        else:
            return None

    def get_auth_handler(self, config):
        username = config.get('username')
        if not username or len(username) <= 0:
            raise PluginError('Username are not configured.')
        password = config.get('password')
        if not password or len(password) <= 0:
            raise PluginError('Password are not configured.')

        db_session = Session()
        cookies = self.try_find_cookie(db_session, username)
        if username not in self.auth_cache:
            auth_handler = LostFilmAuth(username, password, cookies, db_session)
            self.auth_cache[username] = auth_handler
        else:
            auth_handler = self.auth_cache[username]

        return auth_handler

    @plugin.priority(127)
    def on_task_start(self, task, config):
        auth_handler = self.get_auth_handler(config)
        task.requests.auth = auth_handler


# endregion


# region LostFilmPlugin
DOWNLOAD_URL_REGEXP = re.compile(r'^https?://(?:www\.)?lostfilm\.tv/download\.php\?id=(\d+).*$', flags=re.IGNORECASE)
DETAILS_URL_REGEXP = re.compile(r'^https?://(?:www\.)?lostfilm\.tv/details\.php\?id=(\d+).*$', flags=re.IGNORECASE)
REPLACE_DOWNLOAD_URL_REGEXP = re.compile(r'/download\.php', flags=re.IGNORECASE)

SHOW_ALL_RELEASES_REGEXP = re.compile(
    r'ShowAllReleases\(\\?[\'"](.+?)\\?[\'"],\s*\\?[\'"](.+?)\\?[\'"],\s*\\?[\'"](.+?)\\?[\'"]\)',
    flags=re.IGNORECASE)
REPLACE_LOCATION_REGEXP = re.compile(r'location\.replace\([\'"](.+?)[\'"]\);', flags=re.IGNORECASE)


class DbLostFilmShow(Base):
    __tablename__ = 'lostfilm_shows'
    id = Column(Integer, primary_key=True, nullable=False)
    title = Column(Unicode, index=True, nullable=False)
    url = Column(Unicode, nullable=False)
    updated_at = Column(DateTime, nullable=False)


class DbLostFilmShowAlternateName(Base):
    __tablename__ = 'lostfilm_show_alternate_names'
    id = Column(Integer, primary_key=True, autoincrement=True, nullable=False)
    show_id = Column(Integer, ForeignKey('lostfilm_shows.id'), nullable=False)
    title = Column(Unicode, index=True, nullable=False)
    __table_args__ = (UniqueConstraint('show_id', 'title', name='_show_title_uc'),)


class LostFilmShow(object):
    def __init__(self, show_id, titles, url):
        self.show_id = show_id
        self.titles = titles
        self.url = url


class LostFilmParser(object):
    @staticmethod
    def parse_shows_page(html):
        serials_tree = BeautifulSoup(html, 'html.parser')
        mid_node = serials_tree.find('div', class_='mid')
        if not mid_node:
            log.error("Error while parsing details page: node <div class=`mid`> are not found")
            return None

        shows = set()

        url_regexp = re.compile(r'cat=(\d+)', flags=re.IGNORECASE)
        link_nodes = mid_node.find_all('a', class_='bb_a')
        for link_node in link_nodes:
            category_link = link_node.get('href')
            # category_link = process_url(category_link, default_scheme=url_scheme, default_host=url_host)

            url_match = url_regexp.search(category_link)
            if not url_match:
                continue

            show_id = int(url_match.group(1))

            link_text = link_node.get_text(separator='\n')
            titles = link_text.splitlines()
            if len(titles) <= 0:
                log.error("No titles are found")
                continue

            titles = [x.strip('()') for x in titles]

            # log.debug("Serial `{0}` was added".format(" / ".join(titles)))
            show = LostFilmShow(show_id=show_id, titles=titles, url=category_link)
            shows.add(show)

        log.debug("{0:d} show(s) are found".format(len(shows)))
        return shows


class LostFilmDatabase(object):
    @staticmethod
    def shows_timestamp(db_session):
        shows_timestamp = db_session.query(func.min(DbLostFilmShow.updated_at)).scalar() or None
        return shows_timestamp

    @staticmethod
    def shows_count(db_session):
        return db_session.query(DbLostFilmShow).count()

    @staticmethod
    def clear_shows(db_session):
        db_session.query(DbLostFilmShowAlternateName).delete()
        db_session.query(DbLostFilmShow).delete()
        db_session.commit()

    @staticmethod
    def update_shows(shows, db_session):
        # Clear database
        LostFilmDatabase.clear_shows(db_session)

        # Insert new rows
        if shows and len(shows) > 0:
            now = datetime.now()
            for show in shows:
                db_show = DbLostFilmShow(id=show.show_id, title=show.titles[0], url=show.url, updated_at=now)
                db_session.add(db_show)

                for index, item in enumerate(show.titles[1:], start=1):
                    alternate_name = DbLostFilmShowAlternateName(show_id=show.show_id, title=item)
                    db_session.add(alternate_name)

            db_session.commit()

    @staticmethod
    def get_shows(db_session):
        shows = set()

        db_shows = db_session.query(DbLostFilmShow).all()
        for db_show in db_shows:
            titles = list()
            titles.append(db_show.title)

            db_alternate_names = db_session.query(DbLostFilmShowAlternateName).filter(
                DbLostFilmShowAlternateName.show_id == db_show.id).all()
            if db_alternate_names and len(db_alternate_names) > 0:
                for db_alternate_name in db_alternate_names:
                    titles.append(db_alternate_name.title)

            show = LostFilmShow(show_id=db_show.id, titles=titles, url=db_show.url)
            shows.add(show)

        return shows

    @staticmethod
    def get_show_by_id(show_id, db_session):
        db_show = db_session.query(DbLostFilmShow).filter(DbLostFilmShow.id == show_id).first()
        if db_show:
            titles = list()
            titles.append(db_show.title)

            db_alternate_names = db_session.query(DbLostFilmShowAlternateName).filter(
                DbLostFilmShowAlternateName.show_id == db_show.id).all()
            if db_alternate_names and len(db_alternate_names) > 0:
                for db_alternate_name in db_alternate_names:
                    titles.append(db_alternate_name.title)

            show = LostFilmShow(show_id=db_show.id, titles=titles, url=db_show.url)
            return show

        return None

    @staticmethod
    def find_show_by_title(title, db_session):
        db_show = db_session.query(DbLostFilmShow).filter(DbLostFilmShow.title == title).first()
        if db_show:
            return LostFilmDatabase.get_show_by_id(db_show.id, db_session)

        db_alternate_name = db_session.query(DbLostFilmShowAlternateName).filter(
            DbLostFilmShowAlternateName.title == title).first()
        if db_alternate_name:
            return LostFilmDatabase.get_show_by_id(db_alternate_name.show_id, db_session)

        return None


class LostFilmPlugin(object):
    """
        LostFilm urlrewrite/search plugin.

        Example::

          lostfilm:
            regexp: '1080p'
        """

    config_ = {}

    schema = {
        'oneOf': [
            {'type': 'boolean'},
            {
                'type': 'object',
                'properties': {
                    'regexp': {'type': 'string', 'format': 'regex', 'default': '*'}
                },
                'additionalProperties': False
            }
        ]
    }

    def on_task_start(self, task, config):
        if not isinstance(config, dict):
            log.verbose("Config was not determined - use default.")
        else:
            self.config_ = config

    def url_rewritable(self, task, entry):
        url = entry['url']
        if DOWNLOAD_URL_REGEXP.match(url):
            return True
        if DETAILS_URL_REGEXP.match(url):
            return True

        return False

    def get_response(self, task, url):
        response = task.requests.get(url)
        response_content = response.content

        response_html = response_content.decode(response.encoding)
        replace_location_match = REPLACE_LOCATION_REGEXP.search(response_html)
        if replace_location_match:
            replace_location_url = replace_location_match.group(1)
            replace_location_url = process_url(replace_location_url, response.url)
            log.debug("`location.replace(...)` has been detected! Redirecting from `{0}` to `{1}`...".format(
                url, replace_location_url))
            response = task.requests.get(replace_location_url)

        return response

    def url_rewrite(self, task, entry):
        details_url = entry['url']

        log.debug("Starting with url `{0}`...".format(details_url))

        # Convert download url to details if needed
        if DOWNLOAD_URL_REGEXP.match(details_url):
            details_url = REPLACE_DOWNLOAD_URL_REGEXP.sub('/details.php', details_url)
            log.debug("Rewrite url to `{0}`".format(details_url))

        log.debug("Fetching details page `{0}`...".format(details_url))

        try:
            details_response = task.requests.get(details_url)
        except requests.RequestException as e:
            reject_reason = "Error while fetching page: {0}".format(e)
            log.error(reject_reason)
            entry.reject(reject_reason)
            sleep(3)
            return False
        details_html = details_response.content
        sleep(3)

        log.debug("Parsing details page `{0}`...".format(details_url))

        details_tree = BeautifulSoup(details_html, 'html.parser')
        mid_node = details_tree.find('div', class_='mid')
        if not mid_node:
            reject_reason = "Error while parsing details page: node <div class=`mid`> are not found"
            log.error(reject_reason)
            entry.reject(reject_reason)
            return False

        onclick_node = mid_node.find('a', class_='a_download', onclick=SHOW_ALL_RELEASES_REGEXP)
        if not onclick_node:
            reject_reason = "Error while parsing details page: node <a class=`a_download`> are not found"
            log.error(reject_reason)
            entry.reject(reject_reason)
            return False

        onclick_match = SHOW_ALL_RELEASES_REGEXP.search(onclick_node.get('onclick'))
        if not onclick_match:
            reject_reason = "Error while parsing details page: " \
                            "node <a class=`a_download`> have invalid `onclick` attribute"
            log.error(reject_reason)
            entry.reject(reject_reason)
            return False
        category = onclick_match.group(1)
        season = onclick_match.group(2)
        episode = onclick_match.group(3)
        torrents_url = "http://www.lostfilm.tv/nrdr2.php?c={0}&s={1}&e={2}".format(category, season, episode)

        log.debug(u"Downloading torrents page `{0}`...".format(torrents_url))

        try:
            torrents_response = self.get_response(task, torrents_url)
        except requests.RequestException as e:
            reject_reason = "Error while fetching page: {0}".format(e)
            log.error(reject_reason)
            entry.reject(reject_reason)
            sleep(3)
            return False
        torrents_html = torrents_response.content
        sleep(3)

        text_pattern = self.config_.get('regexp', '*')
        text_regexp = re.compile(text_pattern, flags=re.IGNORECASE)

        log.debug("Parsing torrent links...")

        torrents_tree = BeautifulSoup(torrents_html, 'html.parser')
        table_nodes = torrents_tree.find_all('table')
        for table_node in table_nodes:
            link_node = table_node.find('a')
            if link_node:
                torrent_link = link_node.get('href')
                description_text = link_node.get_text()
                if text_regexp.search(description_text):
                    log.debug("Torrent link was accepted! [ regexp: `{0}`, description: `{1}` ]".format(
                        text_pattern, description_text))
                    entry['url'] = torrent_link
                    return True
                else:
                    log.debug("Torrent link was rejected: [ regexp: `{0}`, description: `{1}` ]".format(
                        text_pattern, description_text))

        reject_reason = "Torrent link was not detected with regexp `{0}`".format(text_pattern)
        log.error(reject_reason)
        entry.reject(reject_reason)
        return False

    def get_shows(self, task):
        serials_url = 'http://www.lostfilm.tv/serials.php'

        log.debug("Fetching serials page `{0}`...".format(serials_url))

        try:
            serials_response = task.requests.get(serials_url)
        except requests.RequestException as e:
            log.error("Error while fetching page: {0}".format(e))
            sleep(3)
            return None
        serials_html = serials_response.content
        sleep(3)

        log.debug("Parsing serials page `{0}`...".format(serials_url))

        shows = LostFilmParser.parse_shows_page(serials_html)
        if shows:
            for show in shows:
                show.url = process_url(show.url, serials_response.url)

        return shows

    def search_show(self, task, title, db_session):
        update_required = True
        db_timestamp = LostFilmDatabase.shows_timestamp(db_session)
        if db_timestamp:
            difference = datetime.now() - db_timestamp
            update_required = difference.days > 3
        if update_required:
            log.debug('Update shows...')
            shows = self.get_shows(task)
            if shows:
                log.debug('{0} show(s) received'.format(len(shows)))
                LostFilmDatabase.update_shows(shows, db_session)

        show = LostFilmDatabase.find_show_by_title(title, db_session)
        return show

    def search(self, task, entry, config=None):
        entries = set()

        db_session = Session()

        ep_regexp = re.compile(r"(\d+)\s+[Сс]езон\s+(\d+)\s+[Сс]ерия", flags=re.IGNORECASE)
        row_regexp = re.compile(r't_row.*', flags=re.IGNORECASE)
        search_regexp = re.compile(r'^(.*?)\s*s(\d+?)e(\d+?)$', flags=re.IGNORECASE)

        for search_string in entry.get('search_strings', [entry['title']]):
            search_match = search_regexp.search(search_string)
            if not search_match:
                continue

            search_title = search_match.group(1)
            search_season = int(search_match.group(2))
            search_episode = int(search_match.group(3))

            log.debug("{0} s{1:02d}e{2:02d}".format(search_title, search_season, search_episode))

            show = self.search_show(task, search_title, db_session)
            if not show:
                continue

            try:
                category_response = task.requests.get(show.url)
            except requests.RequestException as e:
                log.error("Error while fetching page: {0}".format(e))
                sleep(3)
                continue
            category_html = category_response.content
            sleep(3)

            category_tree = BeautifulSoup(category_html, 'html.parser')
            mid_node = category_tree.find('div', class_='mid')

            row_nodes = mid_node.find_all('div', class_=row_regexp)
            for row_node in row_nodes:
                ep_node = row_node.find('span', class_='micro')
                if not ep_node:
                    continue

                ep_match = ep_regexp.search(ep_node.get_text())
                if not ep_match:
                    continue

                season = int(ep_match.group(1))
                episode = int(ep_match.group(2))
                if season != search_season or episode != search_episode:
                    continue

                details_node = row_node.find('a', class_='a_details')
                if not details_node:
                    continue

                details_url = details_node.get('href')
                details_url = process_url(details_url, category_response.url)

                entry = Entry()
                entry['title'] = "{0} / s{1:02d}e{2:02d}".format(search_title, season, episode)
                # entry['series_season'] = season
                # entry['series_episode'] = episode
                entry['url'] = details_url
                # tds = link.parent.parent.parent.find_all('td')
                # entry['torrent_seeds'] = int(tds[-2].contents[0])
                # entry['torrent_leeches'] = int(tds[-1].contents[0])
                # entry['search_sort'] = torrent_availability(entry['torrent_seeds'], entry['torrent_leeches'])
                # Parse content_size
                # size = link.find_next(attrs={'class': 'detDesc'}).get_text()
                # size = re.search('Size (\d+(\.\d+)?\xa0(?:[PTGMK])iB)', size)
                #
                # entry['content_size'] = parse_filesize(size.group(1))

                entries.add(entry)

        return entries


# endregion


@event('plugin.register')
def register_plugin():
    plugin.register(LostFilmAuthPlugin, 'lostfilm_auth', api_ver=2)
    plugin.register(LostFilmPlugin, PLUGIN_NAME, groups=['urlrewriter', 'search'], api_ver=2)
