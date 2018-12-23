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

PLUGIN_NAME = 'baibako'
SCHEMA_VER = 0

DOMAIN = 'baibako.tv'
BASE_URL = 'http://baibako.tv'

log = logging.getLogger(PLUGIN_NAME)
Base = versioned_base(PLUGIN_NAME, SCHEMA_VER)


def process_url(url, base_url):
    return urljoin(base_url, url)


# region BaibakoAuthPlugin
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


class BaibakoAccount(Base):
    __tablename__ = 'baibako_accounts'
    id = Column(Integer, primary_key=True, autoincrement=True, nullable=False)
    username = Column(Unicode, index=True, nullable=False, unique=True)
    cookies = Column(JSONEncodedDict)
    expiry_time = Column(DateTime, nullable=False)


class BaibakoAuth(AuthBase):
    """
    Supports downloading of torrents from 'baibako' tracker
    if you pass cookies (CookieJar) to constructor then authentication will be bypassed
    and cookies will be just set
    """

    def try_authenticate(self, payload):
        for _ in range(5):
            session = requests.Session()
            session.post('{0}/takelogin.php'.format(BASE_URL), data=payload)
            cookies = session.cookies.get_dict(domain=DOMAIN)
            if cookies and len(cookies) > 0 and 'uid' in cookies:
                return cookies
            sleep(3)
        raise PluginError('Unable to obtain cookies from Baibako. Looks like invalid username or password.')

    def __init__(self, username, password, cookies=None, db_session=None):
        if cookies is None:
            log.debug('Baibako cookie not found. Requesting new one.')
            payload_ = {'username': username, 'password': password}
            self.cookies_ = self.try_authenticate(payload_)
            if db_session:
                db_session.add(
                    BaibakoAccount(
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

    def try_find_cookie(self, db_session, username):
        account = db_session.query(BaibakoAccount).filter(BaibakoAccount.username == username).first()
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
            auth_handler = BaibakoAuth(username, password, cookies, db_session)
            self.auth_cache[username] = auth_handler
        else:
            auth_handler = self.auth_cache[username]

        return auth_handler

    @plugin.priority(127)
    def on_task_start(self, task, config):
        auth_handler = self.get_auth_handler(config)
        task.requests.auth = auth_handler


# endregion


# region BaibakoPlugin
TABLE_CLASS_REGEXP = re.compile(r'table.*', flags=re.IGNORECASE)

FORUM_ID_REGEXP = re.compile(r'serial\.php\?id=(\d+)', flags=re.IGNORECASE)
TOPIC_ID_REGEXP = re.compile(r'details\.php\?id=(\d+)', flags=re.IGNORECASE)

TOPIC_TITLE_REGEXP = re.compile(
    r'^(?P<title>[^/]*?)\s*/\s*(?P<title_orig>[^/]*?)\s*/\s*s(?P<season>\d+)(?:e(?P<episode_begin>\d+)(?:-(?P<episode_end>\d+))?)?\s*/\s*(?P<quality>[^/]*?)\s*(?:(?:/.*)|$)',
    flags=re.IGNORECASE)


class BaibakoForum(object):
    def __init__(self, id_, title):
        self.id = id_
        self.title = title


class BaibakoTopic(object):
    def __init__(self, id_, title):
        self.id = id_
        self.title = title


class BaibakoTopicInfo(object):
    def __init__(self, title, alternative_titles, season, begin_episode, end_episode, quality):
        self.title = title
        self.alternative_titles = alternative_titles
        self.season = season
        self.begin_episode = begin_episode
        self.end_episode = max([end_episode, begin_episode])
        self.quality = quality

    def get_episode_id(self):
        if self.begin_episode <= 0:
            return 'S{0:02d}'.format(self.season)
        if self.end_episode <= self.begin_episode:
            return 'S{0:02d}E{1:02d}'.format(self.season, self.begin_episode)
        return 'S{0:02d}E{1:02d}-{2:02d}'.format(self.season, self.begin_episode, self.end_episode)

    def contains_episode(self, episode):
        return (episode >= self.begin_episode) and (episode <= self.end_episode)


class ParsingError(Exception):
    def __init__(self, message):
        self.message = message

    def __str__(self):
        return "{0}".format(self.message)

    def __unicode__(self):
        return u"{0}".format(self.message)


class BaibakoParser(object):
    @staticmethod
    def parse_forums(html):
        soup = BeautifulSoup(html, 'html.parser')
        table_node = soup.find('table', class_=TABLE_CLASS_REGEXP)
        if not table_node:
            raise Exception('Node <table class=`table.*`> are not found')

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
    def parse_topics(html):
        soup = BeautifulSoup(html, 'html.parser')
        table_node = soup.find('table', class_=TABLE_CLASS_REGEXP)
        if not table_node:
            raise Exception('Node <table class=`table.*`> are not found')

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
    def parse_topic_title(title):
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


class DbBaibakoTopic(Base):
    __tablename__ = 'baibako_topics'
    id = Column(Integer, primary_key=True, nullable=False)
    forum_id = Column(Integer, nullable=False)
    title = Column(Unicode, nullable=False)
    updated_at = Column(DateTime, nullable=False)


class BaibakoDatabase(object):
    @staticmethod
    def forums_timestamp(db_session):
        timestamp = db_session.query(func.min(DbBaibakoForum.updated_at)).scalar() or None
        return timestamp

    @staticmethod
    def forums_count(db_session):
        return db_session.query(DbBaibakoForum).count()

    @staticmethod
    def clear_forums(db_session):
        db_session.query(DbBaibakoForum).delete()
        db_session.commit()

    @staticmethod
    def update_forums(forums, db_session):
        # Clear database
        BaibakoDatabase.clear_forums(db_session)

        # Insert new rows
        if forums and len(forums) > 0:
            now = datetime.now()
            for forum in forums:
                db_forum = DbBaibakoForum(id=forum.id, title=forum.title, updated_at=now)
                db_session.add(db_forum)

            db_session.commit()

    @staticmethod
    def get_forums(db_session):
        forums = set()

        db_forums = db_session.query(DbBaibakoForum).all()
        for db_forum in db_forums:
            forums.add(BaibakoForum(db_forum.id, db_forum.title))

        return forums

    @staticmethod
    def get_forum_by_id(forum_id, db_session):
        db_forum = db_session.query(DbBaibakoForum).filter(DbBaibakoForum.id == forum_id).first()
        if db_forum:
            return BaibakoForum(db_forum.id, db_forum.title)

        return None

    @staticmethod
    def find_forum_by_title(title, db_session):
        db_forum = db_session.query(DbBaibakoForum).filter(DbBaibakoForum.title == title).first()
        if db_forum:
            return BaibakoForum(db_forum.id, db_forum.title)

        return None

    @staticmethod
    def forum_topics_timestamp(forum_id, db_session):
        topics_timestamp = db_session.query(func.min(DbBaibakoTopic.updated_at)).filter(
            DbBaibakoTopic.forum_id == forum_id).scalar() or None
        return topics_timestamp

    @staticmethod
    def forum_topics_count(forum_id, db_session):
        return db_session.query(DbBaibakoTopic).filter(DbBaibakoTopic.forum_id == forum_id).count()

    @staticmethod
    def clear_forum_topics(forum_id, db_session):
        db_session.query(DbBaibakoTopic).filter(DbBaibakoTopic.forum_id == forum_id).delete()
        db_session.commit()

    @staticmethod
    def update_forum_topics(forum_id, topics, db_session):
        # Clear database
        BaibakoDatabase.clear_forum_topics(forum_id, db_session)

        # Insert new rows
        if topics and len(topics) > 0:
            now = datetime.now()
            for topic in topics:
                db_topic = DbBaibakoTopic(id=topic.id, forum_id=forum_id, title=topic.title, updated_at=now)
                db_session.add(db_topic)

            db_session.commit()

    @staticmethod
    def get_forum_topics(forum_id, db_session):
        topics = set()

        db_topics = db_session.query(DbBaibakoTopic).filter(DbBaibakoTopic.forum_id == forum_id)
        for db_topic in db_topics:
            topic = BaibakoTopic(db_topic.id, db_topic.title)
            topics.add(topic)

        return topics


class Baibako(object):
    @staticmethod
    def get_forum_url(forum_id, tab='all'):
        return '{0}/serial.php?id={1}&tab={2}'.format(BASE_URL, forum_id, tab)

    @staticmethod
    def get_topic_url(topic_id):
        return '{0}/details.php?id={1}'.format(BASE_URL, topic_id)

    @staticmethod
    def get_download_url(topic_id):
        return '{0}/download.php?id={1}'.format(BASE_URL, topic_id)

    @staticmethod
    def get_forums(requests_):
        url = '{0}/serials.php'.format(BASE_URL)
        response = requests_.get(url)
        html = response.content
        return BaibakoParser.parse_forums(html)

    @staticmethod
    def get_forum_topics(forum_id, tab, requests_):
        url = '{0}/serial.php?id={1}&tab={2}'.format(BASE_URL, forum_id, tab)
        response = requests_.get(url)
        html = response.content
        return BaibakoParser.parse_topics(html)


FORUMS_CACHE_DAYS_LIFETIME = 3
FORUM_TOPICS_CACHE_DAYS_LIFETIME = 1


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

    def url_rewritable(self, task, entry):
        url = entry['url']
        match = TOPIC_ID_REGEXP.search(url)
        if match:
            return True
        return False

    def url_rewrite(self, task, entry):
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

    def _search_forum(self, task, title, db_session):
        update_required = True
        db_timestamp = BaibakoDatabase.forums_timestamp(db_session)
        if db_timestamp:
            difference = datetime.now() - db_timestamp
            update_required = difference.days > FORUMS_CACHE_DAYS_LIFETIME
        if update_required:
            log.debug('Update forums...')
            shows = Baibako.get_forums(task.requests)
            if shows:
                log.debug('{0} forum(s) received'.format(len(shows)))
                BaibakoDatabase.update_forums(shows, db_session)

        return BaibakoDatabase.find_forum_by_title(title, db_session)

    def _search_forum_topics(self, task, forum_id, tab, db_session):
        update_required = True
        db_timestamp = BaibakoDatabase.forum_topics_timestamp(forum_id, db_session)
        if db_timestamp:
            difference = datetime.now() - db_timestamp
            update_required = difference.days > FORUM_TOPICS_CACHE_DAYS_LIFETIME
        if update_required:
            log.debug('Update topics for forum `{0}`...'.format(forum_id))
            topics = Baibako.get_forum_topics(forum_id, tab, task.requests)
            if topics:
                log.debug('{0} topic(s) received for forum `{1}`'.format(len(topics), forum_id))
                BaibakoDatabase.update_forum_topics(forum_id, topics, db_session)
                return topics

        return BaibakoDatabase.get_forum_topics(forum_id, db_session)

    def search(self, task, entry, config=None):
        entries = set()

        db_session = Session()

        serial_tab = config.get('serial_tab', 'all')

        search_string_regexp = re.compile(r'^(.*?)\s*s(\d+)e(\d+)$', flags=re.IGNORECASE)

        for search_string in entry.get('search_strings', [entry['title']]):
            search_match = search_string_regexp.search(search_string)
            if not search_match:
                continue

            search_title = search_match.group(1)
            search_season = int(search_match.group(2))
            search_episode = int(search_match.group(3))

            log.debug("{0} s{1:02d}e{2:02d}".format(search_title, search_season, search_episode))

            forum = self._search_forum(task, search_title, db_session)
            if not forum:
                continue

            topics = self._search_forum_topics(task, forum.id, serial_tab, db_session)
            for topic in topics:
                try:
                    topic_info = BaibakoParser.parse_topic_title(topic.title)
                except ParsingError as e:
                    log.warn(e)
                else:
                    if topic_info.season == search_season and topic_info.contains_episode(search_episode):
                        episode_id = topic_info.get_episode_id()

                        entry = Entry()
                        entry['title'] = "{0} / {1} / {2}".format(
                            search_title, episode_id, topic_info.quality)
                        entry['url'] = Baibako.get_download_url(topic.id)
                        # entry['series_season'] = topic_info.season
                        # entry['series_episode'] = topic_info.begin_episode
                        entry['series_id'] = episode_id
                        # entry['quality'] = topic_info.quality

                        entries.add(entry)

        return entries


# endregion


def reset_cache(manager):
    db_session = Session()
    db_session.query(DbBaibakoTopic).delete()
    db_session.query(DbBaibakoForum).delete()
    # db_session.query(LostFilmAccount).delete()
    db_session.commit()

    console('The BaibaKo cache has been reset')


def do_cli(manager, options_):
    with manager.acquire_lock():
        if options_.lf_action == 'reset_cache':
            reset_cache(manager)


@event('plugin.register')
def register_plugin():
    # Register CLI commands
    parser = options.register_command(PLUGIN_NAME, do_cli, help='Utilities to manage the BaibaKo plugin')
    subparsers = parser.add_subparsers(title='Actions', metavar='<action>', dest='lf_action')
    subparsers.add_parser('reset_cache', help='Reset the BaibaKo cache')

    plugin.register(BaibakoAuthPlugin, PLUGIN_NAME + '_auth', api_ver=2)
    plugin.register(BaibakoPlugin, PLUGIN_NAME, groups=['urlrewriter', 'search'], api_ver=2)
