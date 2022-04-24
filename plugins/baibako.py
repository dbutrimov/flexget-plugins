# -*- coding: utf-8 -*-

import hashlib
import logging
import re
from datetime import datetime, timedelta
from time import sleep
from typing import Dict, Text, Optional, Set, List, Any

import bencodepy
from bs4 import BeautifulSoup
from flexget import options
from flexget import plugin
from flexget.db_schema import versioned_base
from flexget.entry import Entry
from flexget.event import event
from flexget.manager import Session, Manager
from flexget.plugin import PluginError
from flexget.task import Task
from flexget.terminal import console
from requests import Session as RequestsSession, PreparedRequest
from requests.auth import AuthBase
from sqlalchemy import Column, Unicode, Integer, DateTime, func
from sqlalchemy.orm import Session as OrmSession

from .utils import ContentType, JSONEncodedDict

PLUGIN_NAME = 'baibako'
SCHEMA_VER = 0

log = logging.getLogger(PLUGIN_NAME)
Base = versioned_base(PLUGIN_NAME, SCHEMA_VER)

BASE_URL = 'http://baibako.tv'
COOKIES_DOMAIN = 'baibako.tv'

HOST_REGEXP = re.compile(r'^https?://(?:www\.)?(?:.+\.)?baibako\.tv', flags=re.IGNORECASE)

USER_AGENT = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/45.0.2454.85 Safari/537.36'


def validate_host(url: Text) -> bool:
    return HOST_REGEXP.match(url) is not None


# region BaibakoAuthPlugin
class BaibakoAccount(Base):
    __tablename__ = 'baibako_accounts'
    id = Column(Integer, primary_key=True, autoincrement=True, nullable=False)
    username = Column(Unicode, index=True, nullable=False, unique=True)
    cookies = Column(JSONEncodedDict)
    expiry_time = Column(DateTime, nullable=False)

    def __init__(self, username: str, cookies: dict, expiry_time: datetime) -> None:
        self.username = username
        self.cookies = cookies
        self.expiry_time = expiry_time


class BaibakoAuth(AuthBase):
    """
    Supports downloading of torrents from 'baibako' tracker
    if you pass cookies (CookieJar) to constructor then authentication will be bypassed
    and cookies will be just set
    """

    def try_authenticate(self, payload: Dict) -> Dict:
        for _ in range(5):
            with RequestsSession() as session:
                session.headers.update({'User-Agent': USER_AGENT})

                response = session.post('{0}/takelogin.php'.format(BASE_URL), data=payload)
                response.raise_for_status()

                cookies = session.cookies.get_dict(domain=COOKIES_DOMAIN)
                if cookies and len(cookies) > 0 and 'uid' in cookies:
                    return cookies

            sleep(3)

        raise PluginError('Unable to obtain cookies from Baibako. Looks like invalid username or password.')

    def __init__(self, username: Text, password: Text, cookies: Dict = None, session: OrmSession = None) -> None:
        if cookies is None:
            log.debug('Baibako cookie not found. Requesting new one.')
            payload_ = {'username': username, 'password': password}
            self.__cookies = self.try_authenticate(payload_)
            if session:
                session.add(
                    BaibakoAccount(
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

    def __call__(self, request: PreparedRequest) -> PreparedRequest:
        # request.prepare_cookies(self.__cookies)
        if validate_host(request.url):
            request.headers.update({
                'User-Agent': USER_AGENT,
                'Cookie': '; '.join('{0}={1}'.format(key, val) for key, val in self.__cookies.items())
            })
        return request


class BaibakoAuthPlugin(object):
    """Usage:

    baibako_auth:
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
        account = session.query(BaibakoAccount).filter(BaibakoAccount.username == username).first()
        if account:
            if account.expiry_time < datetime.now():
                session.delete(account)
                session.commit()
                return None
            return account.cookies
        else:
            return None

    def get_auth_handler(self, config: Dict) -> BaibakoAuth:
        username = config.get('username')
        if not username or len(username) <= 0:
            raise PluginError('Username are not configured.')
        password = config.get('password')
        if not password or len(password) <= 0:
            raise PluginError('Password are not configured.')

        with Session() as session:
            cookies = self.try_find_cookie(session, username)
            if username not in self.auth_cache:
                auth_handler = BaibakoAuth(username, password, cookies, session)
                self.auth_cache[username] = auth_handler
            else:
                auth_handler = self.auth_cache[username]

            return auth_handler

    @plugin.priority(plugin.PRIORITY_DEFAULT)
    def on_task_start(self, task: Task, config: Dict) -> None:
        task.requests.auth = self.get_auth_handler(config)

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
            entry['download_auth'] = self.get_auth_handler(config)


# endregion


# region BaibakoPlugin
TABLE_CLASS_REGEXP = re.compile(r'table.*', flags=re.IGNORECASE)

FORUM_ID_REGEXP = re.compile(r'serial\.php\?id=(\d+)', flags=re.IGNORECASE)
TOPIC_ID_REGEXP = re.compile(r'details\.php\?id=(\d+)', flags=re.IGNORECASE)

TOPIC_TITLE_REGEXP = re.compile(
    r'^(?P<title>[^/]*?)\s*/\s*(?P<title_orig>[^/]*?)\s*/\s*s(?P<season>\d+)(?:e(?P<episode_begin>\d+)(?:-(?P<episode_end>\d+))?)?\s*/\s*(?P<quality>[^/]*?)\s*(?:(?:/.*)|$)',
    flags=re.IGNORECASE)


class BaibakoForum(object):
    def __init__(self, id_: int, title: Text) -> None:
        self.id = id_
        self.title = title


class BaibakoTopic(object):
    def __init__(self, id_: int, title: Text) -> None:
        self.id = id_
        self.title = title


class BaibakoTopicInfo(object):
    def __init__(self, title: Text, alternative_titles: List[Text],
                 season: int, begin_episode: int, end_episode: int, quality: Text) -> None:
        self.title = title
        self.alternative_titles = alternative_titles
        self.season = season
        self.begin_episode = begin_episode
        self.end_episode = max([end_episode, begin_episode])
        self.quality = quality

    def get_episode_id(self) -> Text:
        if self.begin_episode <= 0:
            return 's{0:02d}'.format(self.season)
        if self.end_episode <= self.begin_episode:
            return 's{0:02d}e{1:02d}'.format(self.season, self.begin_episode)
        return 's{0:02d}e{1:02d}-{2:02d}'.format(self.season, self.begin_episode, self.end_episode)

    def contains_episode(self, episode: int) -> bool:
        return (episode >= self.begin_episode) and (episode <= self.end_episode)


class ParsingError(Exception):
    def __init__(self, message: Text) -> None:
        self.message = message

    def __str__(self) -> Text:
        return "{0}".format(self.message)

    def __unicode__(self) -> Text:
        return u"{0}".format(self.message)


class BaibakoParser(object):
    @staticmethod
    def parse_topic_id(url: Text) -> Optional[int]:
        match = TOPIC_ID_REGEXP.search(url)
        if not match:
            return None
        return int(match.group(1))

    @staticmethod
    def parse_forums(html: Text) -> Set[BaibakoForum]:
        soup = BeautifulSoup(html, 'html.parser')
        table_node = soup.find('div', class_="row serialsearch")
        if not table_node:
            raise ParsingError('Node <div class=`row serialsearch`> are not found')

        forums = set()

        row_nodes = table_node.find_all('a', href=FORUM_ID_REGEXP)
        for row_node in row_nodes:
            forum_url = row_node.get('href')
            url_match = FORUM_ID_REGEXP.search(forum_url)
            if not url_match:
                continue

            forum_id = int(url_match.group(1))
            forum_title = row_node.text

            forums.add(BaibakoForum(forum_id, forum_title))

        return forums

    @staticmethod
    def parse_topics(html: Text) -> Set[BaibakoTopic]:
        soup = BeautifulSoup(html, 'html.parser')
        table_node = soup.find('table', class_=TABLE_CLASS_REGEXP)
        if not table_node:
            raise ParsingError('Node <table class=`table.*`> are not found')

        topics = set()

        row_nodes = table_node.find_all('a', href=TOPIC_ID_REGEXP)
        for row_node in row_nodes:
            topic_url = row_node.get('href')
            url_match = TOPIC_ID_REGEXP.search(topic_url)
            if not url_match:
                continue

            # entry_title = "{0} / {1} / {2} / {3}".format(title, ru_title, episode_id, quality)

            topic_id = int(url_match.group(1))
            topic_title = row_node.text

            topics.add(BaibakoTopic(topic_id, topic_title))

        return topics

    @staticmethod
    def parse_topic_title(title: Text) -> BaibakoTopicInfo:
        match = TOPIC_TITLE_REGEXP.search(title)
        if not match:
            raise ParsingError("Title `{0}` has invalid format".format(title))

        season = int(match.group('season'))

        try:
            begin_episode = int(match.group('episode_begin'))
        except Exception:
            begin_episode = 0

        try:
            end_episode = int(match.group('episode_end'))
        except Exception:
            end_episode = begin_episode

        title = match.group('title')
        alternative_title = match.group('title_orig')
        quality = match.group('quality')

        return BaibakoTopicInfo(title, [alternative_title], season, begin_episode, end_episode, quality)


class DbBaibakoForum(Base):
    __tablename__ = 'baibako_forums'
    id = Column(Integer, primary_key=True, nullable=False)
    title = Column(Unicode, index=True, nullable=False)
    updated_at = Column(DateTime, nullable=False)

    def __init__(self, id_: int, title: str, updated_at: datetime) -> None:
        self.id = id_
        self.title = title
        self.updated_at = updated_at


class DbBaibakoTopic(Base):
    __tablename__ = 'baibako_topics'
    id = Column(Integer, primary_key=True, nullable=False)
    forum_id = Column(Integer, nullable=False)
    title = Column(Unicode, nullable=False)
    updated_at = Column(DateTime, nullable=False)

    def __init__(self, id_: int, forum_id: int, title: str, updated_at: datetime) -> None:
        self.id = id_
        self.forum_id = forum_id
        self.title = title
        self.updated_at = updated_at


class BaibakoDatabase(object):
    @staticmethod
    def forums_timestamp(session: OrmSession) -> datetime:
        return session.query(func.min(DbBaibakoForum.updated_at)).scalar() or None

    @staticmethod
    def forums_count(session: OrmSession) -> int:
        return session.query(DbBaibakoForum).count()

    @staticmethod
    def clear_forums(session: OrmSession) -> None:
        session.query(DbBaibakoForum).delete()
        session.commit()

    @staticmethod
    def update_forums(forums: Set[BaibakoForum], session: OrmSession) -> None:
        # Clear database
        BaibakoDatabase.clear_forums(session)

        # Insert new rows
        if forums and len(forums) > 0:
            now = datetime.now()
            for forum in forums:
                db_forum = DbBaibakoForum(id_=forum.id, title=forum.title, updated_at=now)
                session.add(db_forum)

            session.commit()

    @staticmethod
    def get_forums(session: OrmSession) -> Set[BaibakoForum]:
        forums = set()

        db_forums = session.query(DbBaibakoForum).all()
        for db_forum in db_forums:
            forums.add(BaibakoForum(db_forum.id, db_forum.title))

        return forums

    @staticmethod
    def get_forum_by_id(forum_id: int, session: OrmSession) -> Optional[BaibakoForum]:
        db_forum = session.query(DbBaibakoForum).filter(DbBaibakoForum.id == forum_id).first()
        if db_forum:
            return BaibakoForum(db_forum.id, db_forum.title)

        return None

    @staticmethod
    def find_forum_by_title(title: Text, session: OrmSession) -> Optional[BaibakoForum]:
        db_forum = session.query(DbBaibakoForum).filter(DbBaibakoForum.title == title).first()
        if db_forum:
            return BaibakoForum(db_forum.id, db_forum.title)

        return None

    @staticmethod
    def forum_topics_timestamp(forum_id: int, session: OrmSession) -> datetime:
        return session.query(func.min(DbBaibakoTopic.updated_at)).filter(
            DbBaibakoTopic.forum_id == forum_id).scalar() or None

    @staticmethod
    def forum_topics_count(forum_id: int, session: OrmSession) -> int:
        return session.query(DbBaibakoTopic).filter(DbBaibakoTopic.forum_id == forum_id).count()

    @staticmethod
    def clear_forum_topics(forum_id: int, session: OrmSession) -> None:
        session.query(DbBaibakoTopic).filter(DbBaibakoTopic.forum_id == forum_id).delete()
        session.commit()

    @staticmethod
    def update_forum_topics(forum_id: int, topics: Set[BaibakoTopic], session: OrmSession) -> None:
        # Clear database
        BaibakoDatabase.clear_forum_topics(forum_id, session)

        # Insert new rows
        if topics and len(topics) > 0:
            now = datetime.now()
            for topic in topics:
                db_topic = DbBaibakoTopic(id_=topic.id, forum_id=forum_id, title=topic.title, updated_at=now)
                session.add(db_topic)

            session.commit()

    @staticmethod
    def get_forum_topics(forum_id: int, session: OrmSession) -> Set[BaibakoTopic]:
        topics = set()

        db_topics = session.query(DbBaibakoTopic).filter(DbBaibakoTopic.forum_id == forum_id)
        for db_topic in db_topics:
            topic = BaibakoTopic(db_topic.id, db_topic.title)
            topics.add(topic)

        return topics


class Baibako(object):
    @staticmethod
    def get_forum_url(forum_id: int, tab: Text = 'all') -> Text:
        return '{0}/serial.php?id={1}&tab={2}'.format(BASE_URL, forum_id, tab)

    @staticmethod
    def get_topic_url(topic_id: int) -> Text:
        return '{0}/details.php?id={1}'.format(BASE_URL, topic_id)

    @staticmethod
    def get_download_url(topic_id: int) -> Text:
        return '{0}/download.php?id={1}'.format(BASE_URL, topic_id)

    @staticmethod
    def get_forums(requests: RequestsSession) -> Set[BaibakoForum]:
        url = '{0}/serials.php'.format(BASE_URL)
        response = requests.get(url)
        response.raise_for_status()
        try:
            return BaibakoParser.parse_forums(response.text)
        except ParsingError:
            log.error('Parsing failed: {0}'.format(url))
            raise

    @staticmethod
    def get_forum_topics(forum_id: int, tab: Text, requests: RequestsSession) -> Set[BaibakoTopic]:
        url = '{0}/serial.php?id={1}&tab={2}'.format(BASE_URL, forum_id, tab)
        response = requests.get(url)
        response.raise_for_status()
        try:
            return BaibakoParser.parse_topics(response.text)
        except ParsingError:
            log.error('Parsing failed: {0}'.format(url))
            raise

    @staticmethod
    def get_info_hash(requests: RequestsSession, topic_id: int) -> Text:
        download_url = Baibako.get_download_url(topic_id)
        response = requests.get(download_url)
        response.raise_for_status()
        ContentType.raise_not_torrent(response)

        info = bencodepy.decode(response.content)
        return hashlib.sha1(bencodepy.encode(info[b'info'])).hexdigest().lower()


FORUMS_CACHE_DAYS_LIFETIME = 3
FORUM_TOPICS_CACHE_DAYS_LIFETIME = 1
SEARCH_STRING_REGEXPS = [
    re.compile(r'^(.*?)\s*(\d+?)x(\d+?)$', flags=re.IGNORECASE),
    re.compile(r'^(.*?)\s*s(\d+?)e(\d+?)$', flags=re.IGNORECASE)
]


class BaibakoPlugin(object):
    """
    BaibaKo urlrewrite/search plugin.

    Usage:

        baibako:
          serial_tab: 'hd720' or 'hd1080' or 'x264' or 'xvid' or 'all'
    """

    schema = {
        'oneOf': [
            {'type': 'boolean'},
            {
                'type': 'object',
                'properties': {
                    'serial_tab': {'type': 'string', 'default': 'all'}
                },
                'additionalProperties': False
            }
        ]
    }

    def url_rewritable(self, task: Task, entry: Entry) -> bool:
        return BaibakoParser.parse_topic_id(entry['url']) is not None

    def url_rewrite(self, task: Task, entry: Entry) -> bool:
        url = entry['url']
        url_match = TOPIC_ID_REGEXP.search(url)
        if not url_match:
            reject_reason = "Url don't matched: {0}".format(url)
            log.debug(reject_reason)
            # entry.reject(reject_reason)
            return False

        topic_id = int(url_match.group(1))
        entry['url'] = Baibako.get_download_url(topic_id)
        return True

    @plugin.priority(plugin.PRIORITY_LAST)
    def on_task_filter(self, task, config):
        if not config:
            log.debug('Filter disabled, skipping')
            return
        for entry in task.entries:
            url = entry['url']
            topic_id = BaibakoParser.parse_topic_id(url)
            if not topic_id:
                log.debug('Invalid url `{0}`, skipping'.format(url))
                continue
            if 'torrent_info_hash' not in entry:
                log.debug('Entry {0} has no torrent_info_hash, skipping'.format(entry))
                continue
            torrent_info_hash = entry['torrent_info_hash'].lower()
            info_hash = Baibako.get_info_hash(task.requests, topic_id)
            log.debug('Equals hash info {0} with {1}...'.format(torrent_info_hash, info_hash))
            if torrent_info_hash == info_hash:
                entry.reject('Already up-to-date torrent with this infohash')
                continue

            entry['torrent_info_hash'] = info_hash
            entry.accept()

    def _search_forum(self, task: Task, title: Text, session: OrmSession) -> BaibakoForum:
        update_required = True
        db_timestamp = BaibakoDatabase.forums_timestamp(session)
        if db_timestamp:
            difference = datetime.now() - db_timestamp
            update_required = difference.days > FORUMS_CACHE_DAYS_LIFETIME
        if update_required:
            log.debug('Update forums...')
            try:
                shows = Baibako.get_forums(task.requests)
            except Exception as e:
                log.warning(e)
            else:
                if shows:
                    log.debug('{0} forum(s) received'.format(len(shows)))
                    BaibakoDatabase.update_forums(shows, session)

        return BaibakoDatabase.find_forum_by_title(title, session)

    def _search_forum_topics(self, task: Task, forum_id: int, tab: Text, session: OrmSession) -> Set[BaibakoTopic]:
        update_required = True
        db_timestamp = BaibakoDatabase.forum_topics_timestamp(forum_id, session)
        if db_timestamp:
            difference = datetime.now() - db_timestamp
            update_required = difference.days > FORUM_TOPICS_CACHE_DAYS_LIFETIME
        if update_required:
            log.debug('Update topics for forum `{0}`...'.format(forum_id))
            try:
                topics = Baibako.get_forum_topics(forum_id, tab, task.requests)
            except Exception as e:
                log.warning(e)
            else:
                if topics:
                    log.debug('{0} topic(s) received for forum `{1}`'.format(len(topics), forum_id))
                    BaibakoDatabase.update_forum_topics(forum_id, topics, session)
                    return topics

        return BaibakoDatabase.get_forum_topics(forum_id, session)

    def search(self, task: Task, entry: Entry, config: Dict = None) -> Set[Entry]:
        with Session() as session:
            serial_tab = config.get('serial_tab', 'all')

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

                forum = self._search_forum(task, search_title, session)
                if not forum:
                    log.debug("Unknown forum: {0} s{1:02d}e{2:02d}".format(search_title, search_season, search_episode))
                    continue

                topics = self._search_forum_topics(task, forum.id, serial_tab, session)
                for topic in topics:
                    try:
                        topic_info = BaibakoParser.parse_topic_title(topic.title)
                    except ParsingError as e:
                        log.warning(e)
                    else:
                        if topic_info.season != search_season or not topic_info.contains_episode(search_episode):
                            continue

                        episode_id = topic_info.get_episode_id()

                        entry = Entry()
                        entry['title'] = "{0} / {1} / {2}".format(search_title, episode_id, topic_info.quality)
                        entry['url'] = Baibako.get_download_url(topic.id)
                        # entry['series_season'] = topic_info.season
                        # entry['series_episode'] = topic_info.begin_episode
                        entry['series_id'] = episode_id
                        # entry['series_name'] = topic_info.title
                        # entry['quality'] = topic_info.quality

                        entries.add(entry)

            return entries


# endregion


def reset_cache(manager: Manager) -> None:
    with Session() as session:
        session.query(DbBaibakoTopic).delete()
        session.query(DbBaibakoForum).delete()
        # session.query(LostFilmAccount).delete()
        session.commit()

    console('The BaibaKo cache has been reset')


def do_cli(manager: Manager, options_: Any) -> None:
    with manager.acquire_lock():
        if options_.lf_action == 'reset_cache':
            reset_cache(manager)


@event('plugin.register')
def register_plugin() -> None:
    # Register CLI commands
    parser = options.register_command(PLUGIN_NAME, do_cli, help='Utilities to manage the BaibaKo plugin')
    subparsers = parser.add_subparsers(title='Actions', metavar='<action>', dest='lf_action')
    subparsers.add_parser('reset_cache', help='Reset the BaibaKo cache')

    plugin.register(BaibakoAuthPlugin, PLUGIN_NAME + '_auth', api_ver=2)
    plugin.register(BaibakoPlugin, PLUGIN_NAME, interfaces=['urlrewriter', 'search', 'task'], api_ver=2)
