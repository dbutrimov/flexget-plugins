# -*- coding: utf-8 -*-
from __future__ import unicode_literals, division, absolute_import
from builtins import *  # pylint: disable=unused-import, redefined-builtin

import json
import logging
import re
from datetime import datetime, timedelta
from time import sleep
from urllib.parse import urlsplit, urlunsplit

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

plugin_name = 'newstudio'

Base = versioned_base(plugin_name, 0)
log = logging.getLogger(plugin_name)

viewtopic_url_regexp = re.compile(r'^https?://(?:www\.)?newstudio\.tv/viewtopic\.php\?t=(\d+).*$', flags=re.IGNORECASE)
download_url_regexp = re.compile(r'^(?:.*)download.php\?id=(\d+)$', flags=re.IGNORECASE)

url_scheme = 'http'
url_host = 'newstudio.tv'


def process_url(url, default_scheme, default_host):
    split_result = urlsplit(url)
    fragments = list(split_result)
    if len(fragments[0]) <= 0:
        fragments[0] = default_scheme
    if len(fragments[1]) <= 0:
        fragments[1] = default_host
    url = urlunsplit(fragments)
    return url


# region NewStudioAuthPlugin
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


class NewStudioAccount(Base):
    __tablename__ = 'newstudio_accounts'
    id = Column(Integer, primary_key=True, autoincrement=True, nullable=False)
    username = Column(Unicode, index=True, nullable=False, unique=True)
    cookies = Column(JSONEncodedDict)
    expiry_time = Column(DateTime, nullable=False)


class NewStudioAuth(AuthBase):
    """Supports downloading of torrents from 'newstudio' tracker
           if you pass cookies (CookieJar) to constructor then authentication will be bypassed and cookies will be just set
        """

    def try_authenticate(self, payload):
        for _ in range(5):
            session = requests.Session()
            session.post('http://newstudio.tv/login.php', data=payload)
            cookies = session.cookies.get_dict(domain='.newstudio.tv')
            if cookies and len(cookies) > 0:
                return cookies
            sleep(3)
        raise PluginError('Unable to obtain cookies from NewStudio. Looks like invalid username or password.')

    def __init__(self, username, password, cookies=None, db_session=None):
        if cookies is None:
            log.debug('NewStudio cookie not found. Requesting new one.')

            payload_ = {
                'login_username': username,
                'login_password': password,
                'autologin': 1,
                'login': 1
            }

            self.cookies_ = self.try_authenticate(payload_)
            if db_session:
                db_session.add(
                    NewStudioAccount(
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


class NewStudioAuthPlugin(object):
    """Usage:

    newstudio_auth:
      username: 'username_here'
      password: 'password_here'
    """

    schema = {
        'type': 'object',
        'properties': {
            'username': {'type': 'string'},
            'password': {'type': 'string'}
        },
        "additionalProperties": False
    }

    auth_cache = {}

    def try_find_cookie(self, db_session, username):
        account = db_session.query(NewStudioAccount).filter(NewStudioAccount.username == username).first()
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
            auth_handler = NewStudioAuth(username, password, cookies, db_session)
            self.auth_cache[username] = auth_handler
        else:
            auth_handler = self.auth_cache[username]

        return auth_handler

    @plugin.priority(127)
    def on_task_start(self, task, config):
        auth_handler = self.get_auth_handler(config)
        task.requests.auth = auth_handler


# endregion


# region NewStudioPlugin
ep_regexp = re.compile(r"\([Сс]езон\s+(\d+)\W+[Cс]ерия\s+(\d+)\)", flags=re.IGNORECASE)


class DbNewStudioShow(Base):
    __tablename__ = 'newstudio_shows'
    id = Column(Integer, primary_key=True, nullable=False)
    title = Column(Unicode, index=True, nullable=False)
    url = Column(Unicode, nullable=False)
    updated_at = Column(DateTime, nullable=False)


class DbNewStudioShowAlternateName(Base):
    __tablename__ = 'newstudio_show_alternate_names'
    id = Column(Integer, primary_key=True, autoincrement=True, nullable=False)
    show_id = Column(Integer, ForeignKey('newstudio_shows.id'), nullable=False)
    title = Column(Unicode, index=True, nullable=False)
    __table_args__ = (UniqueConstraint('show_id', 'title', name='_show_title_uc'),)


class NewStudioShow(object):
    def __init__(self, show_id, titles, url):
        self.show_id = show_id
        self.titles = titles
        self.url = url


class NewStudioParser(object):
    @staticmethod
    def parse_shows_page(html):
        serials_tree = BeautifulSoup(html, 'html.parser')
        accordion_node = serials_tree.find('div', class_='accordion', id='serialist')
        if not accordion_node:
            log.error("Error while parsing serials page: node <div class=`accordion` id=`serialist`> are not found")
            return None

        shows = set()

        url_regexp = re.compile(r'f=(\d+)', flags=re.IGNORECASE)
        inner_nodes = accordion_node.find_all('div', class_='accordion-inner')
        for inner_node in inner_nodes:
            link_nodes = inner_node.find_all('a')
            for link_node in link_nodes:
                viewforum_link = link_node.get('href')
                viewforum_link = process_url(viewforum_link, default_scheme=url_scheme, default_host=url_host)

                url_match = url_regexp.search(viewforum_link)
                if not url_match:
                    continue

                show_id = int(url_match.group(1))

                title = link_node.text

                show = NewStudioShow(show_id=show_id, titles=[title], url=viewforum_link)
                shows.add(show)

        log.debug("{0:d} shows are found".format(len(shows)))
        return shows


class NewStudioDatabase(object):
    @staticmethod
    def shows_timestamp(db_session):
        shows_timestamp = db_session.query(func.min(DbNewStudioShow.updated_at)).scalar() or None
        return shows_timestamp

    @staticmethod
    def shows_count(db_session):
        return db_session.query(DbNewStudioShow).count()

    @staticmethod
    def clear_shows(db_session):
        db_session.query(DbNewStudioShowAlternateName).delete()
        db_session.query(DbNewStudioShow).delete()
        db_session.commit()

    @staticmethod
    def update_shows(shows, db_session):
        # Clear database
        NewStudioDatabase.clear_shows(db_session)

        # Insert new rows
        if shows and len(shows) > 0:
            now = datetime.now()
            for show in shows:
                db_show = DbNewStudioShow(id=show.show_id, title=show.titles[0], url=show.url, updated_at=now)
                db_session.add(db_show)

                for index, item in enumerate(show.titles[1:], start=1):
                    alternate_name = DbNewStudioShowAlternateName(show_id=show.show_id, title=item)
                    db_session.add(alternate_name)

            db_session.commit()

    @staticmethod
    def get_shows(db_session):
        shows = set()

        db_shows = db_session.query(DbNewStudioShow).all()
        for db_show in db_shows:
            titles = list()
            titles.append(db_show.title)

            db_alternate_names = db_session.query(DbNewStudioShowAlternateName).filter(
                DbNewStudioShowAlternateName.show_id == db_show.id).all()
            if db_alternate_names and len(db_alternate_names) > 0:
                for db_alternate_name in db_alternate_names:
                    titles.append(db_alternate_name.title)

            show = NewStudioShow(show_id=db_show.id, titles=titles, url=db_show.url)
            shows.add(show)

        return shows

    @staticmethod
    def get_show_by_id(show_id, db_session):
        db_show = db_session.query(DbNewStudioShow).filter(DbNewStudioShow.id == show_id).first()
        if db_show:
            titles = list()
            titles.append(db_show.title)

            db_alternate_names = db_session.query(DbNewStudioShowAlternateName).filter(
                DbNewStudioShowAlternateName.show_id == db_show.id).all()
            if db_alternate_names and len(db_alternate_names) > 0:
                for db_alternate_name in db_alternate_names:
                    titles.append(db_alternate_name.title)

            show = NewStudioShow(show_id=db_show.id, titles=titles, url=db_show.url)
            return show

        return None

    @staticmethod
    def find_show_by_title(title, db_session):
        db_show = db_session.query(DbNewStudioShow).filter(DbNewStudioShow.title == title).first()
        if db_show:
            return NewStudioDatabase.get_show_by_id(db_show.id, db_session)

        db_alternate_name = db_session.query(DbNewStudioShowAlternateName).filter(
            DbNewStudioShowAlternateName.title == title).first()
        if db_alternate_name:
            return NewStudioDatabase.get_show_by_id(db_alternate_name.show_id, db_session)

        return None


class NewStudioPlugin(object):
    def url_rewritable(self, task, entry):
        viewtopic_url = entry['url']
        return viewtopic_url_regexp.match(viewtopic_url)

    def url_rewrite(self, task, entry):
        viewtopic_url = entry['url']

        try:
            viewtopic_response = task.requests.get(viewtopic_url)
        except requests.RequestException as e:
            reject_reason = "Error while fetching page: {0}".format(e)
            log.error(reject_reason)
            entry.reject(reject_reason)
            sleep(3)
            return False
        viewtopic_html = viewtopic_response.content
        sleep(3)

        viewtopic_soup = BeautifulSoup(viewtopic_html, 'html.parser')
        download_node = viewtopic_soup.find('a', href=download_url_regexp)
        if download_node:
            torrent_url = download_node.get('href')
            torrent_url = process_url(torrent_url, default_scheme=url_scheme, default_host=url_host)
            entry['url'] = torrent_url
            return True

        reject_reason = "Torrent link was not detected for `{0}`".format(viewtopic_url)
        log.error(reject_reason)
        entry.reject(reject_reason)
        return False

    def get_shows(self, task):
        serials_url = 'http://newstudio.tv/'

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

        shows = NewStudioParser.parse_shows_page(serials_html)
        return shows

    def search_show(self, task, title, db_session):
        update_required = True
        db_timestamp = NewStudioDatabase.shows_timestamp(db_session)
        if db_timestamp:
            difference = datetime.now() - db_timestamp
            update_required = difference.days > 3
        if update_required:
            log.debug('Update shows...')
            shows = self.get_shows(task)
            if shows:
                log.debug('{0} show(s) received'.format(len(shows)))
                NewStudioDatabase.update_shows(shows, db_session)

        show = NewStudioDatabase.find_show_by_title(title, db_session)
        return show

    def search(self, task, entry, config=None):
        entries = set()

        db_session = Session()

        viewtopic_link_regexp = re.compile(r'.*/viewtopic\.php\?t=(\d+).*', flags=re.IGNORECASE)
        pagination_regexp = re.compile(r'pagination.*', flags=re.IGNORECASE)
        quality_regexp = re.compile(r'^.*\)\s*(.*?)$', flags=re.IGNORECASE)
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

            show_pages = [show.url]
            page_index = 0
            while page_index < len(show_pages):
                current_page_index = page_index
                page_index += 1

                page_url = show_pages[current_page_index]
                try:
                    viewforum_response = task.requests.get(page_url)
                except requests.RequestException as e:
                    log.error("Error while fetching page: {0}".format(e))
                    sleep(3)
                    continue
                viewforum_html = viewforum_response.content
                sleep(3)

                viewforum_tree = BeautifulSoup(viewforum_html, 'html.parser')

                if current_page_index < 1:
                    pagination_node = viewforum_tree.find('div', class_=pagination_regexp)
                    if pagination_node:
                        pagination_link_nodes = pagination_node.find_all('a')
                        for pagination_link_node in pagination_link_nodes:
                            page_number_text = pagination_link_node.text
                            try:
                                int(page_number_text)
                            except Exception:
                                continue
                            page_link = pagination_link_node.get('href')
                            page_link = process_url(page_link, default_scheme=url_scheme, default_host=url_host)
                            show_pages.append(page_link)

                accordion_node = viewforum_tree.find('div', class_='accordion-inner')
                if not accordion_node:
                    continue

                row_nodes = accordion_node.find_all('div', class_='row-fluid')
                for row_node in row_nodes:
                    link_node = row_node.find('a', class_='torTopic tt-text', href=viewtopic_link_regexp)
                    if not link_node:
                        continue

                    title = link_node.text
                    ep_match = ep_regexp.search(title)
                    if not ep_match:
                        continue

                    season = int(ep_match.group(1))
                    episode = int(ep_match.group(2))
                    # log.debug("{0} (s{1:02d}e{2:02d})".format(title, season, episode))
                    if season != search_season or episode != search_episode:
                        continue

                    quality = None
                    quality_match = quality_regexp.search(title)
                    if quality_match:
                        quality = quality_match.group(1)

                    torrent_url = link_node.get('href')
                    torrent_url = process_url(torrent_url, default_scheme=url_scheme, default_host=url_host)

                    entry = Entry()
                    entry['title'] = "{0} / s{1:02d}e{2:02d} / {3}".format(search_title, season, episode, quality)
                    entry['url'] = torrent_url

                    entries.add(entry)

        return entries


# endregion


@event('plugin.register')
def register_plugin():
    plugin.register(NewStudioAuthPlugin, 'newstudio_auth', api_ver=2)
    plugin.register(NewStudioPlugin, plugin_name, groups=['urlrewriter', 'search'], api_ver=2)
