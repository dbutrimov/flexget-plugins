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
from flexget import options
from flexget import plugin
from flexget.db_schema import versioned_base
from flexget.entry import Entry
from flexget.event import event
from flexget.terminal import console
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


class LostFilmApi(object):
    API_URL = 'http://lostfilm.tv/ajaxik.php'

    @staticmethod
    def task_post(task, payload):
        return LostFilmApi.requests_post(task.requests, payload)

    @staticmethod
    def requests_post(requests_, payload):
        response = requests_.post(
            LostFilmApi.API_URL,
            data=payload)
        return response

    @staticmethod
    def session_post(session, payload):
        response = session.post(
            LostFilmApi.API_URL,
            data=payload)
        return response


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
            # session.headers['User-Agent'] = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_12_1) ' \
            #                                 'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/54.0.2840.98 Safari/537.36'

            response = LostFilmApi.session_post(session, payload)
            response_json = response.json()
            if 'error' not in response_json and 'success' in response_json and response_json['success']:
                # username = response_json['name']
                cookies = session.cookies.get_dict(domain='.lostfilm.tv')
                if cookies and len(cookies) > 0:
                    return cookies

            sleep(3)

        raise PluginError('Unable to obtain cookies from LostFilm. Looks like invalid username or password.')

    def __init__(self, username, password, cookies=None, db_session=None):
        if cookies is None:
            log.debug('LostFilm cookie not found. Requesting new one.')

            payload_ = {
                'act': 'users',
                'type': 'login',
                'mail': username,
                'pass': password,
                'rem': 1
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


class DbLostFilmEpisode(Base):
    __tablename__ = 'lostfilm_episode'
    id = Column(Integer, primary_key=True, autoincrement=True, nullable=False)
    show_id = Column(Integer, nullable=False)
    season = Column(Integer, nullable=False)
    episode = Column(Integer, nullable=False)
    title = Column(Unicode, nullable=False)
    url = Column(Unicode, nullable=False)
    timestamp = Column(DateTime, nullable=False)
    __table_args__ = (UniqueConstraint('show_id', 'season', 'episode', name='_show_episode_uc'),)


class LostFilmShow(object):
    def __init__(self, show_id, titles, url):
        self.show_id = show_id
        self.titles = titles
        self.url = url


class LostFilmParser(object):
    @staticmethod
    def parse_shows_page(html):
        shows_json = json.loads(html)
        if 'result' in shows_json and shows_json['result'] == 'ok':
            shows = set()
            shows_data = shows_json['data']
            for item in shows_data:
                show_id = int(item['id'])
                title = item['title']
                origin_title = item['title_orig']
                show_url = item['link']

                show = LostFilmShow(show_id=show_id, titles=[title, origin_title], url=show_url)
                shows.add(show)

            log.debug("{0:d} show(s) are found".format(len(shows)))
            return shows

        return None


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

    @staticmethod
    def find_episode(show_id, season, episode, db_session):
        return db_session.query(DbLostFilmEpisode).filter(
            DbLostFilmEpisode.show_id == show_id,
            DbLostFilmEpisode.season == season,
            DbLostFilmEpisode.episode == episode).first()

    @staticmethod
    def insert_episode(show_id, season, episode, title, url, db_session):
        db_episode = LostFilmDatabase.find_episode(show_id, season, episode, db_session)
        now = datetime.now()
        if not db_episode:
            db_episode = DbLostFilmEpisode(
                show_id=show_id,
                season=season,
                episode=episode)
        db_episode.title = title
        db_episode.url = url
        db_episode.timestamp = now

        db_session.add(db_episode)
        db_session.commit()

        return db_episode


EPISODE_URL_REGEXP = re.compile(r'^https?://(?:www\.)?lostfilm\.tv/series/([^/]+?)/season_(\d+)/episode_(\d+).*$', flags=re.IGNORECASE)
PLAY_EPISODE_REGEXP = re.compile(
    r'PlayEpisode\(\\?[\'"](.+?)\\?[\'"],\s*\\?[\'"](.+?)\\?[\'"],\s*\\?[\'"](.+?)\\?[\'"]\)',
    flags=re.IGNORECASE)

REPLACE_LOCATION_REGEXP = re.compile(r'location\.replace\([\'"](.+?)[\'"]\);', flags=re.IGNORECASE)


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

    def on_task_start(self, task, config):
        if not isinstance(config, dict):
            log.verbose("Config was not determined - use default.")
        else:
            self.config_ = config

    def url_rewritable(self, task, entry):
        url = entry['url']
        if EPISODE_URL_REGEXP.match(url):
            return True

        return False

    def url_rewrite(self, task, entry):
        episode_url = entry['url']

        log.debug("Starting with url `{0}`...".format(episode_url))
        log.debug("Fetching episode page `{0}`...".format(episode_url))

        try:
            episode_response = task.requests.get(episode_url)
        except requests.RequestException as e:
            reject_reason = "Error while fetching page: {0}".format(e)
            log.error(reject_reason)
            entry.reject(reject_reason)
            sleep(3)
            return False
        episode_html = episode_response.content
        sleep(3)

        log.debug("Parsing episode page `{0}`...".format(episode_url))

        episode_tree = BeautifulSoup(episode_html, 'html.parser')
        overlay_node = episode_tree.find('div', class_='overlay-pane')
        if not overlay_node:
            reject_reason = "Error while parsing episode page: node <div class=`overlay-pane`> are not found"
            log.error(reject_reason)
            entry.reject(reject_reason)
            return False

        button_node = overlay_node.find('div', class_='external-btn', onclick=PLAY_EPISODE_REGEXP)
        if not button_node:
            reject_reason = "Error while parsing episode page: node <div class=`external-btn`> are not found"
            log.error(reject_reason)
            entry.reject(reject_reason)
            return False

        onclick_match = PLAY_EPISODE_REGEXP.search(button_node.get('onclick'))
        if not onclick_match:
            reject_reason = "Error while parsing episode page: " \
                            "node <div class=`external-btn`> have invalid `onclick` attribute"
            log.error(reject_reason)
            entry.reject(reject_reason)
            return False
        show_id = onclick_match.group(1)
        season = onclick_match.group(2)
        episode = onclick_match.group(3)
        torrents_url = "http://lostfilm.tv/v_search.php?c={0}&s={1}&e={2}".format(show_id, season, episode)

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
        torrents_list_node = torrents_tree.find('div', class_='inner-box--list')
        if torrents_list_node:
            item_nodes = torrents_list_node.find_all('div', class_='inner-box--item')
            for item_node in item_nodes:
                link_node = item_node.find('a')
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
        step = 10
        total = 0

        shows = set()
        while True:
            payload = {
                'act': 'serial',
                'type': 'search',
                'o': total,  # offset
                's': 2,  # alphabetical sorting
                't': 0  # all shows
            }

            response = LostFilmApi.requests_post(task.requests, payload)
            parsed_shows = LostFilmParser.parse_shows_page(response.text)
            count = 0
            if parsed_shows:
                count = len(parsed_shows)
                for show in parsed_shows:
                    show.url = process_url(show.url, response.url)
                    shows.add(show)

            total += count
            if count < step:
                break

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
        goto_regexp = re.compile(r'^goTo\([\'"](.*?)[\'"].*\)$', flags=re.IGNORECASE)
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

            db_episode = LostFilmDatabase.find_episode(show.show_id, search_season, search_episode, db_session)
            if not db_episode:
                seasons_url = urljoin(show.url + '/', 'seasons')
                try:
                    seasons_response = task.requests.get(seasons_url)
                except requests.RequestException as e:
                    log.error("Error while fetching page: {0}".format(e))
                    sleep(3)
                    continue
                seasons_html = seasons_response.content
                sleep(3)

                category_tree = BeautifulSoup(seasons_html, 'html.parser')
                seasons_node = category_tree.find('div', class_='series-block')
                if not seasons_node:
                    log.error("Error while parsing episodes page: node <div class=`series-block`> are not found")
                    continue

                season_nodes = seasons_node.find_all('table', class_='movie-parts-list')
                for season_node in season_nodes:
                    episode_nodes = season_node.find_all('tr')
                    for episode_node in episode_nodes:
                        # Ignore unavailable episodes
                        available = True
                        row_class = episode_node.get('class')
                        if row_class:
                            available = row_class != 'not-available'
                        if not available:
                            continue

                        ep_node = episode_node.find('td', class_='beta')
                        if not ep_node:
                            continue

                        ep_match = ep_regexp.search(ep_node.get_text())
                        if not ep_match:
                            continue

                        season = int(ep_match.group(1))
                        episode = int(ep_match.group(2))

                        onclick = ep_node.get('onclick')
                        goto_match = goto_regexp.search(onclick)
                        if not goto_match:
                            continue

                        episode_title = None
                        title_node = episode_node.find('td', class_='gamma')
                        if title_node:
                            episode_title = title_node.get_text()
                            lines = episode_title.splitlines()
                            episode_titles = set()
                            for line in lines:
                                line = line.strip(' \'"')
                                if len(line) > 0:
                                    episode_titles.add(line)
                            episode_title = ' / '.join(x for x in episode_titles)

                        episode_link = goto_match.group(1)
                        episode_link = process_url(episode_link, seasons_response.url)

                        title = "{0} / s{1:02d}e{2:02d}".format(' / '.join(x for x in show.titles), season, episode)
                        if episode_title and len(episode_title) > 0:
                            title += ' / ' + episode_title

                        db_updated_episode = LostFilmDatabase.insert_episode(
                            show.show_id, season, episode, title, episode_link, db_session)

                        if season == search_season and episode == search_episode:
                            db_episode = db_updated_episode

            if db_episode:
                entry = Entry()
                entry['title'] = db_episode.title
                # entry['series_season'] = season
                # entry['series_episode'] = episode
                entry['url'] = db_episode.url
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


def reset_cache(manager):
    db_session = Session()
    db_session.query(DbLostFilmEpisode).delete()
    db_session.query(DbLostFilmShowAlternateName).delete()
    db_session.query(DbLostFilmShow).delete()
    # db_session.query(LostFilmAccount).delete()
    db_session.commit()

    console('The LostFilm cache has been reset')


def do_cli(manager, options):
    with manager.acquire_lock():
        if options.lf_action == 'reset_cache':
            reset_cache(manager)


@event('plugin.register')
def register_plugin():
    # Register CLI commands
    parser = options.register_command(PLUGIN_NAME, do_cli, help='Utilities to manage the LostFilm plugin')
    subparsers = parser.add_subparsers(title='Actions', metavar='<action>', dest='lf_action')
    subparsers.add_parser('reset_cache', help='Reset the LostFilm cache')

    plugin.register(LostFilmAuthPlugin, 'lostfilm_auth', api_ver=2)
    plugin.register(LostFilmPlugin, PLUGIN_NAME, groups=['urlrewriter', 'search'], api_ver=2)
