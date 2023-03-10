# -*- coding: utf-8 -*-

import json
import logging
import re
from datetime import datetime, timedelta
from time import sleep
from typing import Optional, Text, List, Dict, Any, Set
from urllib.parse import urljoin

import bs4
from flexget import options
from flexget import plugin
from flexget.db_schema import versioned_base
from flexget.entry import Entry
from flexget.event import event
from flexget.manager import Session, Manager
from flexget.plugin import PluginError
from flexget.task import Task
from flexget.terminal import console
from requests import Session as RequestsSession, Response, PreparedRequest
from requests.auth import AuthBase
from sqlalchemy import Column, Unicode, Integer, DateTime, UniqueConstraint, ForeignKey, func
from sqlalchemy.orm import Session as OrmSession

from .utils import JSONEncodedDict

PLUGIN_NAME = 'lostfilm'
SCHEMA_VER = 0

log = logging.getLogger(PLUGIN_NAME)
Base = versioned_base(PLUGIN_NAME, SCHEMA_VER)

BASE_URL = 'https://www.lostfilm.tv'
COOKIES_DOMAIN = '.lostfilm.tv'

HOST_REGEXP = re.compile(r'^https?://(?:www\.)?(?:.+\.)?lostfilm\.tv', flags=re.IGNORECASE)


def validate_host(url: Text) -> bool:
    return HOST_REGEXP.match(url) is not None


class LostFilmAjaxik(object):
    @staticmethod
    def post(requests: RequestsSession, payload: Dict, headers: Dict = None) -> Response:
        return requests.post(BASE_URL + '/ajaxik.php', data=payload, headers=headers)


# region LostFilmAuthPlugin
class LostFilmAccount(Base):
    __tablename__ = 'lostfilm_accounts'
    id = Column(Integer, primary_key=True, autoincrement=True, nullable=False)
    username = Column(Unicode, index=True, nullable=False, unique=True)
    cookies = Column(JSONEncodedDict)
    expiry_time = Column(DateTime, nullable=False)

    def __init__(self, username: str, cookies: dict, expiry_time: datetime) -> None:
        self.username = username
        self.cookies = cookies
        self.expiry_time = expiry_time


class LostFilmAuth(AuthBase):
    """
    Supports downloading of torrents from 'lostfilm' tracker
    if you pass cookies (CookieJar) to constructor then authentication will be bypassed
    and cookies will be just set
    """

    def try_authenticate(self, payload: Dict) -> Dict:
        for _ in range(5):
            headers = {'Referer': BASE_URL + '/login'}
            response = LostFilmAjaxik.post(self.__requests, payload, headers=headers)
            response.raise_for_status()

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
                cookies = response.cookies.get_dict(domain=COOKIES_DOMAIN)
                if cookies and len(cookies) > 0:
                    return cookies

            sleep(3)

        raise PluginError('Unable to obtain cookies from LostFilm. Looks like invalid username or password.')

    def __init__(self, username: Text, password: Text, cookies: Dict = None,
                 requests: RequestsSession = None,
                 session: OrmSession = None) -> None:
        self.__del_requests = requests is None
        self.__requests = requests or RequestsSession()

        if cookies is None:
            log.debug('LostFilm cookie not found. Requesting new one.')

            payload_ = {
                'act': 'users',
                'type': 'login',
                'mail': username,
                'pass': password,
                'need_captcha': '',
                'captcha': '',
                'rem': 1
            }

            self.__cookies = self.try_authenticate(payload_)
            if session:
                session.add(
                    LostFilmAccount(
                        username=username,
                        cookies=self.__cookies,
                        expiry_time=datetime.now() + timedelta(days=1)))
                session.commit()
                # else:
                #     raise ValueError(
                #         'session can not be None if cookies is None')
        else:
            log.debug('Using previously saved cookie.')
            self.__cookies = cookies

    def __del__(self):
        if self.__del_requests:
            del self.__requests
        self.__requests = None

    def __call__(self, request: PreparedRequest) -> PreparedRequest:
        if validate_host(request.url):
            request.prepare_cookies(self.__cookies)
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

    def try_find_cookie(self, session: OrmSession, username: Text) -> Optional[Dict]:
        account = session.query(LostFilmAccount).filter(LostFilmAccount.username == username).first()
        if account:
            if account.expiry_time < datetime.now():
                session.delete(account)
                session.commit()
                return None
            return account.cookies
        else:
            return None

    def get_auth_handler(self, requests: RequestsSession, config: Dict) -> LostFilmAuth:
        username = config.get('username')
        if not username or len(username) <= 0:
            raise PluginError('Username are not configured.')
        password = config.get('password')
        if not password or len(password) <= 0:
            raise PluginError('Password are not configured.')

        with Session() as session:
            cookies = self.try_find_cookie(session, username)
            if username not in self.auth_cache:
                auth_handler = LostFilmAuth(username, password, cookies, requests, session)
                self.auth_cache[username] = auth_handler
            else:
                auth_handler = self.auth_cache[username]

            return auth_handler

    @plugin.priority(plugin.PRIORITY_DEFAULT)
    def on_task_start(self, task: Task, config: Dict) -> None:
        task.requests.auth = self.get_auth_handler(task.requests, config)

    # Run before all downloads
    @plugin.priority(plugin.PRIORITY_FIRST)
    def on_task_download(self, task, config):
        for entry in task.accepted:
            if entry.get('download_auth'):
                log.debug('entry %s already has auth set, skipping', entry)
                continue

            url = entry['url']
            if not validate_host(url):
                log.debug('entry %s has invalid host, skipping', entry)
                continue

            username = config.get('username')
            log.debug('setting auth with username %s', username)
            entry['download_auth'] = self.get_auth_handler(task.requests, config)


# endregion


# region LostFilmPlugin
EP_REGEXP = re.compile(r"(\d+)\s+[Сс]езон\s+(\d+)\s+[Сс]ерия", flags=re.IGNORECASE)
GOTO_REGEXP = re.compile(r'^goTo\([\'"](.*?)[\'"].*\)$', flags=re.IGNORECASE)


# region LostFilmParser
class ParsingError(Exception):
    def __init__(self, message: Text) -> None:
        self.message = message

    def __str__(self):
        return "{0}".format(self.message)

    def __unicode__(self):
        return u"{0}".format(self.message)


class LostFilmShow(object):
    def __init__(self, id_: int, slug: Text, title: Text, alternate_titles: List[Text] = None) -> None:
        self.id = id_
        self.slug = slug
        self.title = title
        self.alternate_titles = alternate_titles


class LostFilmEpisode(object):
    def __init__(self, show_id: int, season: int, episode: int, title: Text = None) -> None:
        self.show_id = show_id
        self.season = season
        self.episode = episode
        self.title = title

    def get_episode_id(self) -> Text:
        return 's{0:02d}e{1:02d}'.format(self.season, self.episode)


class LostFilmTorrent(object):
    def __init__(self, url: Text, title: Text, label: Text = None) -> None:
        self.url = url
        self.title = title
        self.label = label


class LostFilmParser(object):
    @staticmethod
    def parse_shows_json(text: Text) -> List[LostFilmShow]:
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
    def _parse_play_episode_button(node: bs4.Tag) -> LostFilmEpisode:
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
    def _strip_string(value: Text) -> Text:
        return value.strip(' \t\r\n')

    @staticmethod
    def _to_single_line(text: Text, separator: Text = ' / ') -> Text:
        input_lines = text.splitlines()
        lines = list()
        for line in input_lines:
            line = LostFilmParser._strip_string(line)
            if line:
                lines.append(line)

        return separator.join([line for line in lines])

    @staticmethod
    def parse_seasons_page(html: Text) -> List[LostFilmEpisode]:
        category_tree = bs4.BeautifulSoup(html, 'html.parser')
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
    def parse_episode_page(html: Text) -> LostFilmEpisode:
        episode_tree = bs4.BeautifulSoup(html, 'html.parser')
        overlay_node = episode_tree.find('div', class_='overlay-pane')
        if not overlay_node:
            raise ParsingError('Node <div class=`overlay-pane`> are not found')

        episode = LostFilmParser._parse_play_episode_button(overlay_node)

        header_node = episode_tree.find('h1', class_='seria-header')
        if header_node:
            episode.title = LostFilmParser._to_single_line(header_node.text, ' / ')

        return episode

    @staticmethod
    def parse_torrents_page(html: Text) -> List[LostFilmTorrent]:
        torrents_tree = bs4.BeautifulSoup(html, 'html.parser')
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

    def __init__(self, id_: int, slug: str, title: str, updated_at: datetime) -> None:
        self.id = id_
        self.slug = slug
        self.title = title
        self.updated_at = updated_at


class DbLostFilmShowAlternateName(Base):
    __tablename__ = 'lostfilm_show_alternate_names'
    id = Column(Integer, primary_key=True, autoincrement=True, nullable=False)
    show_id = Column(Integer, ForeignKey('lostfilm_shows.id'), nullable=False)
    title = Column(Unicode, index=True, nullable=False)
    __table_args__ = (UniqueConstraint('show_id', 'title', name='_show_title_uc'),)

    def __init__(self, show_id: int, title: str) -> None:
        self.show_id = show_id
        self.title = title


class DbLostFilmEpisode(Base):
    __tablename__ = 'lostfilm_episodes'
    id = Column(Integer, primary_key=True, autoincrement=True, nullable=False)
    show_id = Column(Integer, nullable=False)
    season = Column(Integer, nullable=False)
    episode = Column(Integer, nullable=False)
    title = Column(Unicode, nullable=False)
    updated_at = Column(DateTime, nullable=False)
    __table_args__ = (UniqueConstraint('show_id', 'season', 'episode', name='_uc_show_episode'),)

    def __init__(self, show_id: int, season: int, episode: int, title: str, updated_at: datetime) -> None:
        self.show_id = show_id
        self.season = season
        self.episode = episode
        self.title = title
        self.updated_at = updated_at


class LostFilmDatabase(object):
    @staticmethod
    def shows_timestamp(session: OrmSession) -> datetime:
        return session.query(func.min(DbLostFilmShow.updated_at)).scalar() or None

    @staticmethod
    def shows_count(session: OrmSession) -> int:
        return session.query(DbLostFilmShow).count()

    @staticmethod
    def clear_shows(session: OrmSession) -> None:
        session.query(DbLostFilmShowAlternateName).delete()
        session.query(DbLostFilmShow).delete()
        session.commit()

    @staticmethod
    def update_shows(session: OrmSession, shows: List[LostFilmShow]) -> None:
        # Clear database
        LostFilmDatabase.clear_shows(session)

        # Insert new rows
        if shows and len(shows) > 0:
            now = datetime.now()
            for show in shows:
                db_show = DbLostFilmShow(id_=show.id, slug=show.slug, title=show.title, updated_at=now)
                session.add(db_show)

                if show.alternate_titles:
                    for alternate_title in show.alternate_titles:
                        db_alternate_title = DbLostFilmShowAlternateName(show_id=show.id, title=alternate_title)
                        session.add(db_alternate_title)

            session.commit()

    @staticmethod
    def get_shows(session: OrmSession) -> List[LostFilmShow]:
        shows = list()
        for db_show in session.query(DbLostFilmShow).all():
            alternate_titles = list()
            db_alternate_names = session.query(DbLostFilmShowAlternateName).filter(
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
    def get_show_by_id(session: OrmSession, show_id: int) -> Optional[LostFilmShow]:
        db_show = session.query(DbLostFilmShow).filter(DbLostFilmShow.id == show_id).first()
        if db_show:
            alternate_titles = list()
            db_alternate_names = session.query(DbLostFilmShowAlternateName).filter(
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
    def find_show_by_title(session: OrmSession, title: Text) -> Optional[LostFilmShow]:
        db_show = session.query(DbLostFilmShow).filter(DbLostFilmShow.title == title).first()
        if db_show:
            return LostFilmDatabase.get_show_by_id(session, db_show.id)

        db_alternate_name = session.query(DbLostFilmShowAlternateName).filter(
            DbLostFilmShowAlternateName.title == title).first()
        if db_alternate_name:
            return LostFilmDatabase.get_show_by_id(session, db_alternate_name.show_id)

        return None

    @staticmethod
    def show_episodes_timestamp(session: OrmSession, show_id: int) -> datetime:
        timestamp = session.query(func.min(DbLostFilmEpisode.updated_at)).filter(
            DbLostFilmEpisode.show_id == show_id).scalar() or None
        return timestamp

    @staticmethod
    def clear_show_episodes(session: OrmSession, show_id: int) -> None:
        session.query(DbLostFilmEpisode).filter(DbLostFilmEpisode.show_id == show_id).delete()
        session.commit()

    @staticmethod
    def find_show_episode(session: OrmSession, show_id: int, season: int, episode: int) -> Optional[LostFilmEpisode]:
        db_episode = session.query(DbLostFilmEpisode).filter(
            DbLostFilmEpisode.show_id == show_id,
            DbLostFilmEpisode.season == season,
            DbLostFilmEpisode.episode == episode).first()
        if db_episode:
            return LostFilmEpisode(db_episode.show_id, db_episode.season, db_episode.episode, db_episode.title)

        return None

    @staticmethod
    def update_show_episodes(session: OrmSession, show_id: int, episodes: List[LostFilmEpisode]) -> None:
        # Clear database
        LostFilmDatabase.clear_show_episodes(session, show_id)

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
                session.add(db_episode)

            session.commit()


# endregion


EPISODE_URL_REGEXP = re.compile(
    r'/series/([^/]+?)/season_(\d+)/episode_(\d+)',
    flags=re.IGNORECASE)
PLAY_EPISODE_REGEXP = re.compile(
    # r'PlayEpisode\(\\?[\'"](.+?)\\?[\'"],\s*\\?[\'"](.+?)\\?[\'"],\s*\\?[\'"](.+?)\\?[\'"]\)',
    r'PlayEpisode\(\\?[\'"](\d+)(\d{3})(\d{3})\\?[\'"]\)',
    flags=re.IGNORECASE)

REPLACE_LOCATION_REGEXP = re.compile(r'location\.replace\([\'"](.+?)[\'"]\);', flags=re.IGNORECASE)

SEARCH_STRING_REGEXPS = [
    re.compile(r'^(.*?)\s*(\d+?)x(\d+?)$', flags=re.IGNORECASE),
    re.compile(r'^(.*?)\s*s(\d+?)e(\d+?)$', flags=re.IGNORECASE)
]


class LostFilm(object):
    @staticmethod
    def get_seasons_url(show_slug: Text) -> Text:
        return '{0}/series/{1}/seasons'.format(BASE_URL, show_slug)

    @staticmethod
    def get_episode_url(show_slug: Text, season: int, episode: int) -> Text:
        return '{0}/series/{1}/season_{2}/episode_{3}'.format(
            BASE_URL,
            show_slug,
            season,
            episode
        )

    @staticmethod
    def get_episode_torrents_url(show_id: int, season: int, episode: int) -> Text:
        # return '{0}/v_search.php?c={1}&s={2}&e={3}'.format(
        return '{0}/v_search.php?a={1}{2:03d}{3:03d}'.format(
            BASE_URL,
            show_id,
            season,
            episode
        )

    @staticmethod
    def _get_response(requests: RequestsSession, url: Text) -> Response:
        response = requests.get(url)
        content = response.content

        html = content.decode(response.encoding)
        match = REPLACE_LOCATION_REGEXP.search(html)
        if match:
            redirect_url = match.group(1)
            redirect_url = urljoin(response.url, redirect_url)
            log.debug("`location.replace(...)` has been detected! Redirecting from `{0}` to `{1}`...".format(
                url, redirect_url))
            response = requests.get(redirect_url)

        return response

    @staticmethod
    def get_shows(requests: RequestsSession) -> List[LostFilmShow]:
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

            headers = {'Referer': BASE_URL + '/series/?type=search&s=2&t=0'}

            count = 0
            response = LostFilmAjaxik.post(requests, payload, headers=headers)
            response.raise_for_status()
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
    def get_show_episode(requests: RequestsSession, show_slug: Text, season: int, episode: int) -> LostFilmEpisode:
        url = LostFilm.get_episode_url(show_slug, season, episode)
        response = requests.get(url)
        response.raise_for_status()
        return LostFilmParser.parse_episode_page(response.text)

    @staticmethod
    def get_show_episodes(requests: RequestsSession, show_slug: Text) -> List[LostFilmEpisode]:
        url = LostFilm.get_seasons_url(show_slug)
        response = requests.get(url)
        response.raise_for_status()
        return LostFilmParser.parse_seasons_page(response.text)

    @staticmethod
    def get_episode_torrents(requests: RequestsSession,
                             show_id: int, season: int, episode: int) -> List[LostFilmTorrent]:
        torrents_url = LostFilm.get_episode_torrents_url(show_id, season, episode)
        response = LostFilm._get_response(requests, torrents_url)
        response.raise_for_status()
        return LostFilmParser.parse_torrents_page(response.text)


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

    def on_task_start(self, task: Task, config: Dict = None):
        if not isinstance(config, dict):
            log.debug("Config was not determined - use default.")
            config = dict()
        self._config = config

    def url_rewritable(self, task: Task, entry: Entry) -> bool:
        url = entry['url']
        match = EPISODE_URL_REGEXP.search(url)
        if match:
            return True

        return False

    def url_rewrite(self, task: Task, entry: Entry) -> bool:
        url = entry['url']

        match = EPISODE_URL_REGEXP.search(url)
        if not match:
            reject_reason = "Invalid url format: `{0}`".format(url)
            log.error(reject_reason)
            entry.reject(reject_reason)
            return False

        show_slug = match.group(1)
        season_number = int(match.group(2))
        episode_number = int(match.group(3))

        try:
            episode = LostFilm.get_show_episode(task.requests, show_slug, season_number, episode_number)
        except Exception as e:
            reject_reason = "Error while getting episode by `{0}`: {1}".format(url, e)
            log.error(reject_reason)
            entry.reject(reject_reason)
            sleep(3)
            return False
        sleep(3)

        try:
            torrents = LostFilm.get_episode_torrents(
                task.requests,
                episode.show_id,
                episode.season,
                episode.episode
            )
        except Exception as e:
            reject_reason = "Error while getting torrents by `{0}`: {1}".format(url, e)
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
            url.url, label_pattern, torrents)
        log.error(reject_reason)
        entry.reject(reject_reason)
        return False

    def _search_show(self, task: Task, session: OrmSession, title: Text) -> LostFilmShow:
        update_required = True
        db_timestamp = LostFilmDatabase.shows_timestamp(session)
        if db_timestamp:
            difference = datetime.now() - db_timestamp
            update_required = difference.days > 3
        if update_required:
            log.debug('Update shows...')
            shows = LostFilm.get_shows(task.requests)
            if shows:
                log.debug('{0} show(s) received'.format(len(shows)))
                LostFilmDatabase.update_shows(session, shows)

        return LostFilmDatabase.find_show_by_title(session, title)

    def _search_show_episode(self, task: Task, session:OrmSession,
                             show: LostFilmShow, season: int, episode: int) -> Optional[LostFilmEpisode]:
        update_required = True
        db_timestamp = LostFilmDatabase.show_episodes_timestamp(session, show.id)
        if db_timestamp:
            difference = datetime.now() - db_timestamp
            update_required = difference.days > 1
        if update_required:
            episodes = LostFilm.get_show_episodes(task.requests, show.slug)
            if episodes:
                LostFilmDatabase.update_show_episodes(session, show.id, episodes)

        return LostFilmDatabase.find_show_episode(session, show.id, season, episode)

    def search(self, task: Task, entry: Entry, config: Dict = None) -> Set[Entry]:
        with Session() as session:
            entries = set()
            for search_string in entry.get('search_strings', [entry['title']]):
                search_match = None
                for search_string_regexp in SEARCH_STRING_REGEXPS:
                    search_match = search_string_regexp.search(search_string)
                    if search_match:
                        break

                if not search_match:
                    log.warning("Invalid search string: {0}".format(search_string))
                    continue

                search_title = search_match.group(1)
                search_season = int(search_match.group(2))
                search_episode = int(search_match.group(3))

                log.debug("{0} s{1:02d}e{2:02d}".format(search_title, search_season, search_episode))

                show = self._search_show(task, session, search_title)
                if not show:
                    log.warning("Unknown show: {0}".format(search_title))
                    continue

                episode = self._search_show_episode(task, session, show, search_season, search_episode)
                if not episode:
                    log.debug("Unknown episode: {0} s{1:02d}e{2:02d}".format(search_title, search_season, search_episode))
                    continue

                episode_id = episode.get_episode_id()

                entry = Entry()
                entry['title'] = "{0} / {1} / {2}".format(search_title, episode_id, episode.title)
                entry['url'] = LostFilm.get_episode_url(show.slug, episode.season, episode.episode)
                # entry['series_season'] = episode.season
                # entry['series_episode'] = episode.episode
                entry['series_id'] = episode_id
                # entry['series_name'] = episode.title
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


def reset_cache(manager: Manager) -> None:
    with Session() as session:
        session.query(DbLostFilmEpisode).delete()
        session.query(DbLostFilmShowAlternateName).delete()
        session.query(DbLostFilmShow).delete()
        # session.query(LostFilmAccount).delete()
        session.commit()

    console('The LostFilm cache has been reset')


def do_cli(manager: Manager, options_: Any) -> None:
    with manager.acquire_lock():
        if options_.lf_action == 'reset_cache':
            reset_cache(manager)


@event('plugin.register')
def register_plugin() -> None:
    # Register CLI commands
    parser = options.register_command(PLUGIN_NAME, do_cli, help='Utilities to manage the LostFilm plugin')
    subparsers = parser.add_subparsers(title='Actions', metavar='<action>', dest='lf_action')
    subparsers.add_parser('reset_cache', help='Reset the LostFilm cache')

    plugin.register(LostFilmAuthPlugin, PLUGIN_NAME + '_auth', api_ver=2)
    plugin.register(LostFilmPlugin, PLUGIN_NAME, interfaces=['urlrewriter', 'search', 'task'], api_ver=2)
