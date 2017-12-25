# -*- coding: utf-8 -*-
from __future__ import unicode_literals, division, absolute_import
from builtins import *  # pylint: disable=unused-import, redefined-builtin

import json
import logging
import re
from datetime import datetime, timedelta
from time import sleep
try:
    from urllib.parse import urljoin
except ImportError:
    from urlparse import urljoin

from bs4 import BeautifulSoup
from flexget import plugin
from flexget.db_schema import versioned_base
from flexget.entry import Entry
from flexget.event import event
from flexget.manager import Session
from flexget.plugin import PluginError
from flexget.utils import requests
from requests.auth import AuthBase
from sqlalchemy import Column, Unicode, Integer, DateTime, ForeignKey, func
from sqlalchemy.types import TypeDecorator, VARCHAR

PLUGIN_NAME = 'newstudio'
SCHEMA_VER = 0

log = logging.getLogger(PLUGIN_NAME)
Base = versioned_base(PLUGIN_NAME, SCHEMA_VER)


def process_url(url, base_url):
    return urljoin(base_url, url)


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
        'additionalProperties': False
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
TOPIC_ID_REGEXP = re.compile(r'viewtopic\.php\?t=(\d+)', flags=re.IGNORECASE)
DOWNLOAD_ID_REGEXP = re.compile(r'download\.php\?id=(\d+)', flags=re.IGNORECASE)

PAGINATION_CLASS_REGEXP = re.compile(r'pagination.*', flags=re.IGNORECASE)

TOPIC_TITLE_EPISODE_REGEXP = re.compile(r"\([Сс]езон\s+(\d+)(?:\W+[Сс]ерия\s+(\d+)(?:-(\d+))?)?\)", flags=re.IGNORECASE)
TOPIC_TITLE_QUALITY_REGEXP = re.compile(r'^.*\)\s*(.*?)(?:\s*\|.*)?$', flags=re.IGNORECASE)


class NewStudioForum(object):
    def __init__(self, id_, title):
        self.id = id_
        self.title = title


class NewStudioTopic(object):
    def __init__(self, id_, title, download_id):
        self.id = id_
        self.title = title
        self.download_id = download_id


class NewStudioTopicInfo(object):
    def __init__(self, title, season, begin_episode, end_episode, quality):
        self.title = title
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


class NewStudioParser(object):
    @staticmethod
    def parse_forums(html):
        soup = BeautifulSoup(html, 'html.parser')
        accordion_node = soup.find('div', class_='accordion', id='serialist')
        if not accordion_node:
            raise ParsingError(
                "Error while parsing serials page: node <div class=`accordion` id=`serialist`> are not found"
            )

        forums = set()

        url_regexp = re.compile(r'f=(\d+)', flags=re.IGNORECASE)
        inner_nodes = accordion_node.find_all('div', class_='accordion-inner')
        for inner_node in inner_nodes:
            link_nodes = inner_node.find_all('a')
            for link_node in link_nodes:
                forum_link = link_node.get('href')
                url_match = url_regexp.search(forum_link)
                if not url_match:
                    continue

                forum_id = int(url_match.group(1))
                title = link_node.text

                forum = NewStudioForum(id_=forum_id, title=title)
                forums.add(forum)

        return forums

    @staticmethod
    def parse_forum_pages_count(html):
        pages_count = 0

        soup = BeautifulSoup(html, 'html.parser')
        pagination_node = soup.find('div', class_=PAGINATION_CLASS_REGEXP)
        if pagination_node:
            pagination_nodes = pagination_node.find_all('li')
            for pagination_node in pagination_nodes:
                page_number_text = pagination_node.text
                try:
                    page_number = int(page_number_text)
                except Exception:
                    continue
                else:
                    if page_number > pages_count:
                        pages_count = page_number

        return pages_count

    @staticmethod
    def parse_topics(html):
        topics = set()

        forum_soup = BeautifulSoup(html, 'html.parser')
        accordion_node = forum_soup.find('div', class_='accordion-inner')
        if not accordion_node:
            raise ParsingError(
                "Error while parsing serials page: node <div class=`accordion-inner`> are not found"
            )

        row_nodes = accordion_node.find_all('div', class_='row-fluid')
        for row_node in row_nodes:
            title_node = row_node.find('a', href=TOPIC_ID_REGEXP)
            if not title_node:
                continue

            title = title_node.text

            topic_url = title_node.get('href')
            match = TOPIC_ID_REGEXP.search(topic_url)
            if not match:
                continue
            topic_id = int(match.group(1))

            torrent_node = row_node.find('a', href=DOWNLOAD_ID_REGEXP)
            if not torrent_node:
                continue

            download_url = torrent_node.get('href')
            match = DOWNLOAD_ID_REGEXP.search(download_url)
            if not match:
                continue
            download_id = int(match.group(1))

            topic = NewStudioTopic(id_=topic_id, title=title, download_id=download_id)
            topics.add(topic)

        return topics

    @staticmethod
    def parse_topic_title(title):
        match = TOPIC_TITLE_EPISODE_REGEXP.search(title)
        if not match:
            raise ParsingError("Title `{0}` has invalid format".format(title))

        season = int(match.group(1))

        try:
            begin_episode = int(match.group(2))
        except Exception:
            begin_episode = 0

        try:
            end_episode = int(match.group(3))
        except Exception:
            end_episode = begin_episode

        quality = None
        match = TOPIC_TITLE_QUALITY_REGEXP.search(title)
        if match:
            quality = match.group(1)

        return NewStudioTopicInfo(title=title, season=season,
                                  begin_episode=begin_episode, end_episode=end_episode,
                                  quality=quality)


class DbNewStudioForum(Base):
    __tablename__ = 'newstudio_forums'
    id = Column(Integer, primary_key=True, nullable=False)
    title = Column(Unicode, index=True, nullable=False)
    updated_at = Column(DateTime, nullable=False)


class DbNewStudioTopic(Base):
    __tablename__ = 'newstudio_topics'
    id = Column(Integer, primary_key=True, nullable=False)
    forum_id = Column(Integer, ForeignKey('newstudio_forums.id'), nullable=False)
    title = Column(Unicode, index=True, nullable=False)
    download_id = Column(Integer, nullable=False)
    updated_at = Column(DateTime, nullable=False)


class NewStudioDatabase(object):
    @staticmethod
    def forums_timestamp(db_session):
        shows_timestamp = db_session.query(func.min(DbNewStudioForum.updated_at)).scalar() or None
        return shows_timestamp

    @staticmethod
    def forums_count(db_session):
        return db_session.query(DbNewStudioForum).count()

    @staticmethod
    def clear_forums(db_session):
        db_session.query(DbNewStudioForum).delete()
        db_session.commit()

    @staticmethod
    def update_forums(forums, db_session):
        # Clear database
        NewStudioDatabase.clear_forums(db_session)

        # Insert new rows
        if forums and len(forums) > 0:
            now = datetime.now()
            for forum in forums:
                db_forum = DbNewStudioForum(id=forum.id, title=forum.title, updated_at=now)
                db_session.add(db_forum)

            db_session.commit()

    @staticmethod
    def get_forums(db_session):
        forums = set()

        db_forums = db_session.query(DbNewStudioForum).all()
        for db_forum in db_forums:
            forum = NewStudioForum(id_=db_forum.id, title=db_forum.title)
            forums.add(forum)

        return forums

    @staticmethod
    def get_forum_by_id(forum_id, db_session):
        db_forum = db_session.query(DbNewStudioForum).filter(DbNewStudioForum.id == forum_id).first()
        if db_forum:
            return NewStudioForum(id_=db_forum.id, title=db_forum.title)

        return None

    @staticmethod
    def find_forum_by_title(title, db_session):
        db_forum = db_session.query(DbNewStudioForum).filter(DbNewStudioForum.title == title).first()
        if db_forum:
            return NewStudioForum(id_=db_forum.id, title=db_forum.title)

        return None

    @staticmethod
    def forum_topics_timestamp(forum_id, db_session):
        topics_timestamp = db_session.query(func.min(DbNewStudioTopic.updated_at)).filter(
            DbNewStudioTopic.forum_id == forum_id).scalar() or None
        return topics_timestamp

    @staticmethod
    def forum_topics_count(forum_id, db_session):
        return db_session.query(DbNewStudioTopic).filter(DbNewStudioTopic.forum_id == forum_id).count()

    @staticmethod
    def clear_forum_topics(forum_id, db_session):
        db_session.query(DbNewStudioTopic).filter(DbNewStudioTopic.forum_id == forum_id).delete()
        db_session.commit()

    @staticmethod
    def update_forum_topics(forum_id, topics, db_session):
        # Clear database
        NewStudioDatabase.clear_forum_topics(forum_id, db_session)

        # Insert new rows
        if topics and len(topics) > 0:
            now = datetime.now()
            for topic in topics:
                db_topic = DbNewStudioTopic(id=topic.id, forum_id=forum_id, title=topic.title,
                                            download_id=topic.download_id, updated_at=now)
                db_session.add(db_topic)

            db_session.commit()

    @staticmethod
    def get_forum_topics(forum_id, db_session):
        topics = set()

        db_topics = db_session.query(DbNewStudioTopic).filter(DbNewStudioTopic.forum_id == forum_id)
        for db_topic in db_topics:
            topic = NewStudioTopic(id_=db_topic.id, title=db_topic.title, download_id=db_topic.download_id)
            topics.add(topic)

        return topics


class NewStudio(object):
    base_url = 'http://newstudio.tv'

    @staticmethod
    def get_forum_url(forum_id):
        return '{0}/viewforum.php?f={1}'.format(NewStudio.base_url, forum_id)

    @staticmethod
    def get_topic_url(topic_id):
        return '{0}/viewtopic.php?t={1}'.format(NewStudio.base_url, topic_id)

    @staticmethod
    def get_download_url(download_id):
        return '{0}/download.php?id={1}'.format(NewStudio.base_url, download_id)

    @staticmethod
    def get_forums(requests_):
        response = requests_.get(NewStudio.base_url)
        html = response.content
        return NewStudioParser.parse_forums(html)

    @staticmethod
    def get_forum_topics(forum_id, requests_):
        result = set()
        pages_count = 0
        page_index = 0
        while True:
            url = '{0}&start={1}'.format(NewStudio.get_forum_url(forum_id), page_index * 50)
            response = requests_.get(url)
            html = response.content
            sleep(3)

            if pages_count < 1:
                pages_count = NewStudioParser.parse_forum_pages_count(html)

            topics = NewStudioParser.parse_topics(html)
            for topic in topics:
                result.add(topic)

            page_index += 1
            if page_index >= pages_count:
                break

        return result


SEARCH_STRING_REGEXP = re.compile(r'^(.*?)\s*s(\d+?)e(\d+?)$', flags=re.IGNORECASE)
FORUMS_CACHE_DAYS_LIFETIME = 3
FORUM_TOPICS_CACHE_DAYS_LIFETIME = 1


class NewStudioPlugin(object):
    def url_rewritable(self, task, entry):
        topic_url = entry['url']
        match = TOPIC_ID_REGEXP.search(topic_url)
        if match:
            return True

        return False

    def url_rewrite(self, task, entry):
        topic_url = entry['url']
        topic_url = topic_url + '&__fix403=1'

        try:
            topic_response = task.requests.get(topic_url)
        except requests.RequestException as e:
            reject_reason = "Error while fetching page: {0}".format(e)
            log.error(reject_reason)
            entry.reject(reject_reason)
            sleep(3)
            return False
        topic_html = topic_response.content
        sleep(3)

        topic_soup = BeautifulSoup(topic_html, 'html.parser')
        download_node = topic_soup.find('a', href=DOWNLOAD_ID_REGEXP)
        if download_node:
            download_url = download_node.get('href')
            match = DOWNLOAD_ID_REGEXP.search(download_url)
            if match:
                download_id = int(match.group(1))
                entry['url'] = NewStudio.get_download_url(download_id)
                return True

        reject_reason = "Torrent link was not detected for `{0}`".format(topic_url)
        log.error(reject_reason)
        entry.reject(reject_reason)
        return False

    def _search_forum(self, task, title, db_session):
        update_required = True
        db_timestamp = NewStudioDatabase.forums_timestamp(db_session)
        if db_timestamp:
            difference = datetime.now() - db_timestamp
            update_required = difference.days > FORUMS_CACHE_DAYS_LIFETIME
        if update_required:
            log.debug('Update forums...')
            forums = NewStudio.get_forums(task.requests)
            if forums:
                log.debug('{0} forum(s) received'.format(len(forums)))
                NewStudioDatabase.update_forums(forums, db_session)

        return NewStudioDatabase.find_forum_by_title(title, db_session)

    def _search_forum_topics(self, task, forum_id, db_session):
        update_required = True
        db_timestamp = NewStudioDatabase.forum_topics_timestamp(forum_id, db_session)
        if db_timestamp:
            difference = datetime.now() - db_timestamp
            update_required = difference.days > FORUM_TOPICS_CACHE_DAYS_LIFETIME
        if update_required:
            log.debug('Update topics for forum `{0}`...'.format(forum_id))
            topics = NewStudio.get_forum_topics(forum_id, task.requests)
            if topics:
                log.debug('{0} topic(s) received for forum `{1}`'.format(len(topics), forum_id))
                NewStudioDatabase.update_forum_topics(forum_id, topics, db_session)
                return topics

        return NewStudioDatabase.get_forum_topics(forum_id, db_session)

    def search(self, task, entry, config=None):
        entries = set()

        db_session = Session()

        for search_string in entry.get('search_strings', [entry['title']]):
            search_match = SEARCH_STRING_REGEXP.search(search_string)
            if not search_match:
                continue

            search_title = search_match.group(1)
            search_season = int(search_match.group(2))
            search_episode = int(search_match.group(3))

            log.debug("{0} s{1:02d}e{2:02d}".format(search_title, search_season, search_episode))

            forum = self._search_forum(task, search_title, db_session)
            if not forum:
                continue

            topics = self._search_forum_topics(task, forum.id, db_session)
            for topic in topics:
                try:
                    topic_info = NewStudioParser.parse_topic_title(topic.title)
                except ParsingError as e:
                    log.warn(e)
                else:
                    if topic_info.season == search_season and topic_info.contains_episode(search_episode):
                        episode_id = topic_info.get_episode_id()

                        entry = Entry()
                        entry['title'] = "{0} / {1} / {2}".format(
                            search_title, episode_id, topic_info.quality)
                        entry['url'] = NewStudio.get_download_url(topic.download_id)
                        # entry['series_season'] = topic_info.season
                        # entry['series_episode'] = topic_info.begin_episode
                        entry['series_id'] = episode_id
                        # entry['quality'] = topic_info.quality

                        entries.add(entry)

        return entries


# endregion


@event('plugin.register')
def register_plugin():
    plugin.register(NewStudioAuthPlugin, PLUGIN_NAME + '_auth', api_ver=2)
    plugin.register(NewStudioPlugin, PLUGIN_NAME, groups=['urlrewriter', 'search'], api_ver=2)
