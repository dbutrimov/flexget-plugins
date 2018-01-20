# -*- coding: utf-8 -*-
from __future__ import unicode_literals, division, absolute_import
from builtins import *  # pylint: disable=unused-import, redefined-builtin

import json
import logging
import re
from datetime import datetime, timedelta
from time import sleep
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

try:
    from urllib.parse import urljoin
except ImportError:
    from urlparse import urljoin

PLUGIN_NAME = 'lostfilm'
SCHEMA_VER = 0

BASE_URL = 'http://lostfilm.tv'
API_URL = BASE_URL + '/ajaxik.php'
COOKIES_DOMAIN = '.lostfilm.tv'

log = logging.getLogger(PLUGIN_NAME)
Base = versioned_base(PLUGIN_NAME, SCHEMA_VER)


def process_url(url, base_url):
    """
    :type url: str
    :type base_url: str
    :rtype: str
    """
    return urljoin(base_url, url)


class LostFilmApi(object):
    @staticmethod
    def post(requests_, payload):
        """
        :type requests_: requests.Session
        :type payload: dict
        :rtype: requests.Response
        """
        response = requests_.post(
            API_URL,
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
    """
    Supports downloading of torrents from 'lostfilm' tracker
    if you pass cookies (CookieJar) to constructor then authentication will be bypassed
    and cookies will be just set
    """

    def try_authenticate(self, payload):
        """
        :type payload: dict
        :rtype: dict
        """
        for _ in range(5):
            session = requests.Session()
            # session.headers['User-Agent'] = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_12_1) ' \
            #                                 'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/54.0.2840.98 Safari/537.36'

            response = LostFilmApi.post(session, payload)
            response_json = response.json()
            if 'need_captcha' in response_json and response_json['need_captcha']:
                raise PluginError('Unable to obtain cookies from LostFilm. Captcha is required. '
                                  'Please logout from you account using web browser (Chrome, Firefox, Safari, etc.) '
                                  'and login again with captcha. Then try again.')
            if 'error' in response_json:
                raise PluginError('Unable to obtain cookies from LostFilm. The error was caused: {0}'.format(
                    response_json['error']))

            if 'success' in response_json and response_json['success']:
                # username = response_json['name']
                cookies = session.cookies.get_dict(domain=COOKIES_DOMAIN)
                if cookies and len(cookies) > 0:
                    return cookies

            sleep(3)

        raise PluginError('Unable to obtain cookies from LostFilm. Looks like invalid username or password.')

    def __init__(self, username, password, cookies=None, db_session=None):
        """
        :type username: str
        :type password: str
        :type cookies: dict
        :type db_session: flexget.manager.Session
        """
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
        """
        :type request: requests.Request
        :rtype: requests.Request
        """
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
EP_REGEXP = re.compile(r"(\d+)\s+[Сс]езон\s+(\d+)\s+[Сс]ерия", flags=re.IGNORECASE)
GOTO_REGEXP = re.compile(r'^goTo\([\'"](.*?)[\'"].*\)$', flags=re.IGNORECASE)


# region LostFilmParser
class ParsingError(Exception):
    def __init__(self, message):
        self.message = message

    def __str__(self):
        return "{0}".format(self.message)

    def __unicode__(self):
        return u"{0}".format(self.message)


class LostFilmShow(object):
    def __init__(self, id_, slug, title, alternate_titles=None):
        """
        :type id_: int
        :type slug: str
        :type title: str
        :type alternate_titles: list[str]
        """
        self.id = id_
        self.slug = slug
        self.title = title
        self.alternate_titles = alternate_titles


class LostFilmEpisode(object):
    def __init__(self, show_id, season, episode, title=None):
        """
        :type show_id: int
        :type season: int
        :type episode: int
        :type title: str
        """
        self.show_id = show_id
        self.season = season
        self.episode = episode
        self.title = title


class LostFilmTorrent(object):
    def __init__(self, url, title, label=None):
        """
        :type url: str
        :type title: str
        :type label: str
        """
        self.url = url
        self.title = title
        self.label = label


class LostFilmParser(object):
    @staticmethod
    def parse_shows_json(text):
        """
        :type text: str
        :rtype: list[LostFilmShow]
        """
        json_data = json.loads(text)
        if 'result' not in json_data or json_data['result'] != 'ok':
            raise ParsingError('`result` field is invalid')

        shows = list()
        shows_data = json_data['data']
        for show_data in shows_data:
            show = LostFilmShow(
                int(show_data['id']),
                show_data['alias'],
                show_data['title'],
                [show_data['title_orig']]
            )
            shows.append(show)

        return shows

    @staticmethod
    def _parse_play_episode_button(node):
        """
        :type node: bs4.Tag
        :rtype: LostFilmEpisode
        """
        button_node = node.find('div', class_='external-btn', onclick=PLAY_EPISODE_REGEXP)
        if not button_node:
            raise ParsingError('Node <div class=`external-btn`> are not found')
        onclick = button_node.get('onclick')
        onclick_match = PLAY_EPISODE_REGEXP.search(onclick)
        if not onclick_match:
            raise ParsingError('Node <div class=`external-btn`> have invalid `onclick` attribute')

        show_id = int(onclick_match.group(1))
        season = int(onclick_match.group(2))
        episode = int(onclick_match.group(3))

        return LostFilmEpisode(show_id, season, episode)

    @staticmethod
    def _strip_string(value):
        """
        :type value: str
        :rtype: str
        """
        return value.strip(' \t\r\n')

    @staticmethod
    def _to_single_line(text, separator=' / '):
        """
        :type text: str
        :type separator: str
        :rtype: str
        """
        input_lines = text.splitlines()
        lines = list()
        for line in input_lines:
            line = LostFilmParser._strip_string(line)
            if line:
                lines.append(line)

        return separator.join([line for line in lines])

    @staticmethod
    def parse_seasons_page(html):
        """
        :type html: str
        :rtype: list[LostFilmEpisode]
        """
        category_tree = BeautifulSoup(html, 'html.parser')
        seasons_node = category_tree.find('div', class_='series-block')
        if not seasons_node:
            raise ParsingError('Node <div class=`series-block`> are not found')

        episodes = list()

        season_nodes = seasons_node.find_all('table', class_='movie-parts-list')
        for season_node in season_nodes:
            episode_nodes = season_node.find_all('tr')
            for episode_node in episode_nodes:
                # Check episode availability
                play_node = episode_node.find('td', class_='zeta')
                if not play_node:
                    continue

                try:
                    episode = LostFilmParser._parse_play_episode_button(play_node)
                except Exception:
                    continue

                # Parse episode title
                title_node = episode_node.find('td', class_='gamma')
                if title_node:
                    episode.title = LostFilmParser._to_single_line(title_node.text, ' / ')

                episodes.append(episode)

        return episodes

    @staticmethod
    def parse_episode_page(html):
        """
        :type html: str
        :rtype: LostFilmEpisode
        """
        episode_tree = BeautifulSoup(html, 'html.parser')
        overlay_node = episode_tree.find('div', class_='overlay-pane')
        if not overlay_node:
            raise ParsingError('Node <div class=`overlay-pane`> are not found')

        episode = LostFilmParser._parse_play_episode_button(overlay_node)

        header_node = episode_tree.find('h1', class_='seria-header')
        if header_node:
            episode.title = LostFilmParser._to_single_line(header_node.text, ' / ')

        return episode

    @staticmethod
    def parse_torrents_page(html):
        """
        :type html: str
        :rtype: list[LostFilmTorrent]
        """
        torrents_tree = BeautifulSoup(html, 'html.parser')
        torrents_list_node = torrents_tree.find('div', class_='inner-box--list')
        if not torrents_list_node:
            raise ParsingError('Node <div class=`inner-box--list`> are not found')

        result = list()
        item_nodes = torrents_list_node.find_all('div', class_='inner-box--item')
        for item_node in item_nodes:
            label = None
            label_node = item_node.find('div', class_='inner-box--label')
            if label_node:
                label = LostFilmParser._strip_string(label_node.text)

            link_node = item_node.find('a')
            if link_node:
                url = link_node.get('href')
                title = LostFilmParser._to_single_line(link_node.text, ' ')

                torrent = LostFilmTorrent(url, title, label)
                result.append(torrent)

        return result
# endregion


# region LostFilmDatabase
class DbLostFilmShow(Base):
    __tablename__ = 'lostfilm_shows'
    id = Column(Integer, primary_key=True, nullable=False)
    slug = Column(Unicode, nullable=False)
    title = Column(Unicode, index=True, nullable=False)
    updated_at = Column(DateTime, nullable=False)


class DbLostFilmShowAlternateName(Base):
    __tablename__ = 'lostfilm_show_alternate_names'
    id = Column(Integer, primary_key=True, autoincrement=True, nullable=False)
    show_id = Column(Integer, ForeignKey('lostfilm_shows.id'), nullable=False)
    title = Column(Unicode, index=True, nullable=False)
    __table_args__ = (UniqueConstraint('show_id', 'title', name='_show_title_uc'),)


class DbLostFilmEpisode(Base):
    __tablename__ = 'lostfilm_episodes'
    id = Column(Integer, primary_key=True, autoincrement=True, nullable=False)
    show_id = Column(Integer, nullable=False)
    season = Column(Integer, nullable=False)
    episode = Column(Integer, nullable=False)
    title = Column(Unicode, nullable=False)
    updated_at = Column(DateTime, nullable=False)
    __table_args__ = (UniqueConstraint('show_id', 'season', 'episode', name='_uc_show_episode'),)


class LostFilmDatabase(object):
    @staticmethod
    def shows_timestamp(db_session):
        timestamp = db_session.query(func.min(DbLostFilmShow.updated_at)).scalar() or None
        return timestamp

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
                db_show = DbLostFilmShow(id=show.id, slug=show.slug, title=show.title, updated_at=now)
                db_session.add(db_show)

                if show.alternate_titles:
                    for alternate_title in show.alternate_titles:
                        db_alternate_title = DbLostFilmShowAlternateName(show_id=show.id, title=alternate_title)
                        db_session.add(db_alternate_title)

            db_session.commit()

    @staticmethod
    def get_shows(db_session):
        shows = list()

        db_shows = db_session.query(DbLostFilmShow).all()
        for db_show in db_shows:
            alternate_titles = list()
            db_alternate_names = db_session.query(DbLostFilmShowAlternateName).filter(
                DbLostFilmShowAlternateName.show_id == db_show.id).all()
            if db_alternate_names and len(db_alternate_names) > 0:
                for db_alternate_name in db_alternate_names:
                    alternate_titles.append(db_alternate_name.title)

            shows.append(LostFilmShow(
                id_=db_show.id,
                slug=db_show.slug,
                title=db_show.title,
                alternate_titles=alternate_titles
            ))

        return shows

    @staticmethod
    def get_show_by_id(show_id, db_session):
        db_show = db_session.query(DbLostFilmShow).filter(DbLostFilmShow.id == show_id).first()
        if db_show:
            alternate_titles = list()
            db_alternate_names = db_session.query(DbLostFilmShowAlternateName).filter(
                DbLostFilmShowAlternateName.show_id == db_show.id).all()
            if db_alternate_names and len(db_alternate_names) > 0:
                for db_alternate_name in db_alternate_names:
                    alternate_titles.append(db_alternate_name.title)

            return LostFilmShow(
                id_=db_show.id,
                slug=db_show.slug,
                title=db_show.title,
                alternate_titles=alternate_titles
            )

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
    def show_episodes_timestamp(db_session, show_id):
        timestamp = db_session.query(func.min(DbLostFilmEpisode.updated_at)).filter(
            DbLostFilmEpisode.show_id == show_id).scalar() or None
        return timestamp

    @staticmethod
    def clear_show_episodes(db_session, show_id):
        db_session.query(DbLostFilmEpisode).filter(DbLostFilmEpisode.show_id == show_id).delete()
        db_session.commit()

    @staticmethod
    def find_show_episode(show_id, season, episode, db_session):
        return db_session.query(DbLostFilmEpisode).filter(
            DbLostFilmEpisode.show_id == show_id,
            DbLostFilmEpisode.season == season,
            DbLostFilmEpisode.episode == episode).first()

    # @staticmethod
    # def insert_show_episode(show_id, season, episode, title, db_session):
    #     db_episode = LostFilmDatabase.find_show_episode(show_id, season, episode, db_session)
    #     now = datetime.now()
    #     if not db_episode:
    #         db_episode = DbLostFilmEpisode(
    #             show_id=show_id,
    #             season=season,
    #             episode=episode)
    #     db_episode.title = title
    #     db_episode.timestamp = now
    #
    #     db_session.add(db_episode)
    #     db_session.commit()
    #
    #     return db_episode

    @staticmethod
    def update_show_episodes(show_id, episodes, db_session):
        # Clear database
        LostFilmDatabase.clear_show_episodes(db_session, show_id)

        # Insert new rows
        if episodes and len(episodes) > 0:
            now = datetime.now()
            for episode in episodes:
                if episode.show_id != show_id:
                    continue

                db_episode = DbLostFilmEpisode(
                    show_id=show_id,
                    season=episode.season,
                    episode=episode.episode,
                    title=episode.title,
                    updated_at=now
                )
                db_session.add(db_episode)

            db_session.commit()

# endregion


EPISODE_URL_REGEXP = re.compile(
    r'^https?://(?:www\.)?lostfilm\.tv/series/([^/]+?)/season_(\d+)/episode_(\d+).*$',
    flags=re.IGNORECASE)
PLAY_EPISODE_REGEXP = re.compile(
    r'PlayEpisode\(\\?[\'"](.+?)\\?[\'"],\s*\\?[\'"](.+?)\\?[\'"],\s*\\?[\'"](.+?)\\?[\'"]\)',
    flags=re.IGNORECASE)

REPLACE_LOCATION_REGEXP = re.compile(r'location\.replace\([\'"](.+?)[\'"]\);', flags=re.IGNORECASE)

SEARCH_REGEXP = re.compile(r'^(.*?)\s*s(\d+?)e(\d+?)$', flags=re.IGNORECASE)


class LostFilm(object):
    @staticmethod
    def get_seasons_url(show_slug):
        return '{0}/series/{1}/seasons'.format(BASE_URL, show_slug)

    @staticmethod
    def get_episode_url(show_slug, season_number, episode_number):
        return '{0}/series/{1}/season_{2}/episode_{3}'.format(
            BASE_URL,
            show_slug,
            season_number,
            episode_number
        )

    @staticmethod
    def get_episode_torrents_url(show_id, season_number, episode_number):
        return '{0}/v_search.php?c={1}&s={2}&e={3}'.format(
            BASE_URL,
            show_id,
            season_number,
            episode_number
        )

    @staticmethod
    def _get_response(requests_, url):
        response = requests_.get(url)
        content = response.content

        html = content.decode(response.encoding)
        match = REPLACE_LOCATION_REGEXP.search(html)
        if match:
            redirect_url = match.group(1)
            redirect_url = process_url(redirect_url, response.url)
            log.debug("`location.replace(...)` has been detected! Redirecting from `{0}` to `{1}`...".format(
                url, redirect_url))
            response = requests_.get(redirect_url)

        return response

    @staticmethod
    def get_shows(requests_):
        step = 10
        total = 0

        shows = list()
        while True:
            payload = {
                'act': 'serial',
                'type': 'search',
                'o': total,  # offset
                's': 2,  # alphabetical sorting
                't': 0  # all shows
            }

            count = 0
            response = LostFilmApi.post(requests_, payload)
            parsed_shows = LostFilmParser.parse_shows_json(response.text)
            if parsed_shows:
                count = len(parsed_shows)
                for show in parsed_shows:
                    shows.append(show)

            total += count
            if count < step:
                break

        return shows

    @staticmethod
    def get_show_episodes(show_slug, requests_):
        url = LostFilm.get_seasons_url(show_slug)
        response = requests_.get(url)
        html = response.content
        return LostFilmParser.parse_seasons_page(html)

    @staticmethod
    def get_episode_torrents(show_id, season, episode, requests_):
        torrents_url = LostFilm.get_episode_torrents_url(show_id, season, episode)
        response = LostFilm._get_response(requests_, torrents_url)
        return LostFilmParser.parse_torrents_page(response.content)


class LostFilmPlugin(object):
    """
        LostFilm urlrewrite/search plugin.

        Example::

          lostfilm:
            label: '1080'  # SD / 1080 / MP4 / $regex
        """

    schema = {
        'oneOf': [
            {'type': 'boolean'},
            {
                'type': 'object',
                'properties': {
                    'label': {'type': 'string', 'format': 'regex', 'default': '*'}
                },
                'additionalProperties': False
            }
        ]
    }

    def __init__(self):
        self._config = None

    def on_task_start(self, task, config):
        if not isinstance(config, dict):
            log.debug("Config was not determined - use default.")
            self._config = dict()
        else:
            self._config = config

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
            reject_reason = "Error while fetching page `{0}`: {1}".format(episode_url, e)
            log.error(reject_reason)
            entry.reject(reject_reason)
            sleep(3)
            return False
        episode_html = episode_response.content
        sleep(3)

        log.debug("Parsing episode page `{0}`...".format(episode_url))

        try:
            episode_data = LostFilmParser.parse_episode_page(episode_html)
        except Exception as e:
            reject_reason = "Error while parsing episode page `{0}`: {1}".format(episode_url, e)
            log.error(reject_reason)
            entry.reject(reject_reason)
            return False

        # log.debug("Downloading torrents page `{0}`...".format(torrents_url))

        try:
            torrents = LostFilm.get_episode_torrents(
                episode_data.show_id,
                episode_data.season,
                episode_data.episode,
                task.requests
            )
        except requests.RequestException as e:
            reject_reason = "Error while getting torrents by `{0}`: {1}".format(episode_url, e)
            log.error(reject_reason)
            entry.reject(reject_reason)
            sleep(3)
            return False
        sleep(3)

        label_pattern = self._config.get('label', '*')
        label_regexp = re.compile(label_pattern, flags=re.IGNORECASE)
        for torrent in torrents:
            if label_regexp.search(torrent.label):
                log.debug("Torrent link was accepted! [ regexp: `{0}`, label: `{1}` ]".format(
                    label_pattern, torrent.label))
                entry['url'] = torrent.url
                return True
            else:
                log.debug("Torrent link was rejected: [ regexp: `{0}`, label: `{1}` ]".format(
                    label_pattern, torrent.label))

        reject_reason = "Torrent link was not detected by `{0}` with regexp `{1}`: {2}".format(
            episode_url.url, label_pattern, torrents)
        log.error(reject_reason)
        entry.reject(reject_reason)
        return False

    def _search_show(self, task, title, db_session):
        update_required = True
        db_timestamp = LostFilmDatabase.shows_timestamp(db_session)
        if db_timestamp:
            difference = datetime.now() - db_timestamp
            update_required = difference.days > 3
        if update_required:
            log.debug('Update shows...')
            shows = LostFilm.get_shows(task.requests)
            if shows:
                log.debug('{0} show(s) received'.format(len(shows)))
                LostFilmDatabase.update_shows(shows, db_session)

        show = LostFilmDatabase.find_show_by_title(title, db_session)
        return show

    def _search_show_episode(self, task, show, season_number, episode_number, db_session):
        update_required = True
        db_timestamp = LostFilmDatabase.show_episodes_timestamp(db_session, show.id)
        if db_timestamp:
            difference = datetime.now() - db_timestamp
            update_required = difference.days > 1
        if update_required:
            episodes = LostFilm.get_show_episodes(show.slug, task.requests)
            if episodes:
                LostFilmDatabase.update_show_episodes(show.id, episodes, db_session)

        episode = LostFilmDatabase.find_show_episode(show.id, season_number, episode_number, db_session)
        return episode

    def search(self, task, entry, config=None):
        entries = set()

        db_session = Session()

        for search_string in entry.get('search_strings', [entry['title']]):
            search_match = SEARCH_REGEXP.search(search_string)
            if not search_match:
                continue

            search_title = search_match.group(1)
            search_season = int(search_match.group(2))
            search_episode = int(search_match.group(3))

            log.debug("{0} s{1:02d}e{2:02d}".format(search_title, search_season, search_episode))

            show = self._search_show(task, search_title, db_session)
            if not show:
                continue

            episode = self._search_show_episode(task, show, search_season, search_episode, db_session)
            if episode:
                entry = Entry()
                entry['title'] = episode.title
                # entry['series_season'] = season
                # entry['series_episode'] = episode
                entry['url'] = episode.url
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


def do_cli(manager, options_):
    with manager.acquire_lock():
        if options_.lf_action == 'reset_cache':
            reset_cache(manager)


@event('plugin.register')
def register_plugin():
    # Register CLI commands
    parser = options.register_command(PLUGIN_NAME, do_cli, help='Utilities to manage the LostFilm plugin')
    subparsers = parser.add_subparsers(title='Actions', metavar='<action>', dest='lf_action')
    subparsers.add_parser('reset_cache', help='Reset the LostFilm cache')

    plugin.register(LostFilmAuthPlugin, PLUGIN_NAME + '_auth', api_ver=2)
    plugin.register(LostFilmPlugin, PLUGIN_NAME, groups=['urlrewriter', 'search'], api_ver=2)
