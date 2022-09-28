# -*- coding: utf-8 -*-

import logging
import re
from datetime import datetime, timedelta
from time import sleep, time
from typing import Optional, Text, Dict, Set
from urllib.parse import urlparse, urlunparse, urlencode, parse_qsl

from bs4 import BeautifulSoup
from flexget import plugin
from flexget.db_schema import versioned_base
from flexget.entry import Entry
from flexget.event import event
from flexget.manager import Session
from flexget.plugin import PluginError
from flexget.task import Task
from requests import Session as RequestsSession, PreparedRequest, RequestException
from requests.auth import AuthBase
from sqlalchemy import Column, Unicode, Integer, DateTime, ForeignKey, func
from sqlalchemy.orm import Session as OrmSession

from .utils import JSONEncodedDict

PLUGIN_NAME = 'newstudio'
SCHEMA_VER = 0

log = logging.getLogger(PLUGIN_NAME)
Base = versioned_base(PLUGIN_NAME, SCHEMA_VER)

BASE_URL = 'http://newstudio.tv'
COOKIES_DOMAIN = '.newstudio.tv'

HOST_REGEXP = re.compile(r'^https?://(?:www\.)?(?:.+\.)?newstudio\.tv', flags=re.IGNORECASE)


def validate_host(url: Text) -> bool:
    return HOST_REGEXP.match(url) is not None


# region NewStudioAuthPlugin
class NewStudioAccount(Base):
    __tablename__ = 'newstudio_accounts'
    id = Column(Integer, primary_key=True, autoincrement=True, nullable=False)
    username = Column(Unicode, index=True, nullable=False, unique=True)
    cookies = Column(JSONEncodedDict)
    expiry_time = Column(DateTime, nullable=False)

    def __init__(self, username: str, cookies: dict, expiry_time: datetime) -> None:
        self.username = username
        self.cookies = cookies
        self.expiry_time = expiry_time


class NewStudioAuth(AuthBase):
    """
    Supports downloading of torrents from 'newstudio' tracker
    if you pass cookies (CookieJar) to constructor then authentication will be bypassed
    and cookies will be just set
    """

    def try_authenticate(self, payload: Dict) -> Dict:
        for _ in range(5):
            with RequestsSession() as session:
                response = session.post('{0}/login.php'.format(BASE_URL), data=payload)
                response.raise_for_status()

                cookies = session.cookies.get_dict(domain=COOKIES_DOMAIN)
                if cookies and len(cookies) > 0:
                    return cookies

            sleep(3)

        raise PluginError('Unable to obtain cookies from NewStudio. Looks like invalid username or password.')

    def __init__(self, username: Text, password: Text, cookies: Dict = None, session: OrmSession = None) -> None:
        if cookies is None:
            log.debug('NewStudio cookie not found. Requesting new one.')

            payload_ = {
                'login_username': username,
                'login_password': password,
                'autologin': 1,
                'login': 1
            }

            self.__cookies = self.try_authenticate(payload_)
            if session:
                session.add(
                    NewStudioAccount(
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
            request.headers['Cookie'] = '; '.join('{0}={1}'.format(key, val) for key, val in self.__cookies.items())
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

    def try_find_cookie(self, session: OrmSession, username: Text) -> Optional[Dict]:
        account = session.query(NewStudioAccount).filter(NewStudioAccount.username == username).first()
        if account:
            if account.expiry_time < datetime.now():
                session.delete(account)
                session.commit()
                return None
            return account.cookies
        else:
            return None

    def get_auth_handler(self, config: Dict) -> Dict:
        username = config.get('username')
        if not username or len(username) <= 0:
            raise PluginError('Username are not configured.')
        password = config.get('password')
        if not password or len(password) <= 0:
            raise PluginError('Password are not configured.')

        with Session() as session:
            cookies = self.try_find_cookie(session, username)
            if username not in self.auth_cache:
                auth_handler = NewStudioAuth(username, password, cookies, session)
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


# region NewStudioPlugin
TOPIC_ID_REGEXP = re.compile(r'viewtopic\.php\?t=(\d+)', flags=re.IGNORECASE)
DOWNLOAD_ID_REGEXP = re.compile(r'download\.php\?id=(\d+)', flags=re.IGNORECASE)

PAGINATION_CLASS_REGEXP = re.compile(r'pagination.*', flags=re.IGNORECASE)

TOPIC_TITLE_EPISODE_REGEXP = re.compile(r"\([Сс]езон\s+(\d+)(?:\W+[Сс]ерия\s+(\d+)(?:-(\d+))?)?\)", flags=re.IGNORECASE)
TOPIC_TITLE_QUALITY_REGEXP = re.compile(r'^.*\)\s*(.*?)(?:\s*\|.*)?$', flags=re.IGNORECASE)


class NewStudioForum(object):
    def __init__(self, id_: int, title: Text) -> None:
        self.id = id_
        self.title = title


class NewStudioTopic(object):
    def __init__(self, id_: int, title: Text, download_id: int) -> None:
        self.id = id_
        self.title = title
        self.download_id = download_id


class NewStudioTopicInfo(object):
    def __init__(self, title: Text, season: int, begin_episode: int, end_episode: int, quality: Text) -> None:
        self.title = title
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


class NewStudioParser(object):
    @staticmethod
    def parse_forums(html: Text) -> Set[NewStudioForum]:
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
    def parse_forum_pages_count(html: Text) -> int:
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
    def parse_topics(html: Text) -> Set[NewStudioTopic]:
        topics = set()

        forum_soup = BeautifulSoup(html, 'html.parser')
        leftside_node = forum_soup.find('div', id='sideLeft')
        if not leftside_node:
            raise ParsingError("Error while parsing serials page: node <div id=`sideLeft`> are not found")

        accordion_node = leftside_node.find('div', class_='accordion-inner')
        if not accordion_node:
            raise ParsingError("Error while parsing serials page: node <div class=`accordion-inner`> are not found")

        row_nodes = accordion_node.find_all('div', class_='row-fluid')
        for row_node in row_nodes:
            title_node = row_node.find('a', href=TOPIC_ID_REGEXP)
            if not title_node:
                continue

            title = title_node.text
            if not title:
                raise ParsingError("Error while parsing serials page: empty title")

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
    def parse_topic_title(title: Text) -> NewStudioTopicInfo:
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

    def __init__(self, id_: int, title: str, updated_at: datetime) -> None:
        self.id = id_
        self.title = title
        self.updated_at = updated_at


class DbNewStudioTopic(Base):
    __tablename__ = 'newstudio_topics'
    id = Column(Integer, primary_key=True, nullable=False)
    forum_id = Column(Integer, ForeignKey('newstudio_forums.id'), nullable=False)
    title = Column(Unicode, index=True, nullable=False)
    download_id = Column(Integer, nullable=False)
    updated_at = Column(DateTime, nullable=False)

    def __init__(self, id_: int, forum_id: int, title: str, download_id: int, updated_at: datetime) -> None:
        self.id = id_
        self.forum_id = forum_id
        self.title = title
        self.download_id = download_id
        self.updated_at = updated_at


class NewStudioDatabase(object):
    @staticmethod
    def forums_timestamp(session: OrmSession) -> datetime:
        return session.query(func.min(DbNewStudioForum.updated_at)).scalar() or None

    @staticmethod
    def forums_count(session: OrmSession) -> int:
        return session.query(DbNewStudioForum).count()

    @staticmethod
    def clear_forums(session: OrmSession) -> None:
        session.query(DbNewStudioForum).delete()
        session.commit()

    @staticmethod
    def update_forums(forums: Set[NewStudioForum], session: OrmSession) -> None:
        # Clear database
        NewStudioDatabase.clear_forums(session)

        # Insert new rows
        if forums and len(forums) > 0:
            now = datetime.now()
            for forum in forums:
                db_forum = DbNewStudioForum(id_=forum.id, title=forum.title, updated_at=now)
                session.add(db_forum)

            session.commit()

    @staticmethod
    def get_forums(session: OrmSession) -> Set[NewStudioForum]:
        forums = set()

        db_forums = session.query(DbNewStudioForum).all()
        for db_forum in db_forums:
            forum = NewStudioForum(id_=db_forum.id, title=db_forum.title)
            forums.add(forum)

        return forums

    @staticmethod
    def get_forum_by_id(forum_id: int, session: OrmSession) -> Optional[NewStudioForum]:
        db_forum = session.query(DbNewStudioForum).filter(DbNewStudioForum.id == forum_id).first()
        if db_forum:
            return NewStudioForum(id_=db_forum.id, title=db_forum.title)

        return None

    @staticmethod
    def find_forum_by_title(title: Text, session: OrmSession) -> Optional[NewStudioForum]:
        db_forum = session.query(DbNewStudioForum).filter(DbNewStudioForum.title == title).first()
        if db_forum:
            return NewStudioForum(id_=db_forum.id, title=db_forum.title)

        return None

    @staticmethod
    def forum_topics_timestamp(forum_id: int, session: OrmSession) -> datetime:
        return session.query(func.min(DbNewStudioTopic.updated_at)).filter(
            DbNewStudioTopic.forum_id == forum_id).scalar() or None

    @staticmethod
    def forum_topics_count(forum_id: int, session: OrmSession) -> int:
        return session.query(DbNewStudioTopic).filter(DbNewStudioTopic.forum_id == forum_id).count()

    @staticmethod
    def clear_forum_topics(forum_id: int, session: OrmSession) -> None:
        session.query(DbNewStudioTopic).filter(DbNewStudioTopic.forum_id == forum_id).delete()
        session.commit()

    @staticmethod
    def update_forum_topics(forum_id: int, topics: Set[NewStudioTopic], session: OrmSession) -> None:
        # Clear database
        NewStudioDatabase.clear_forum_topics(forum_id, session)

        # Insert new rows
        if topics and len(topics) > 0:
            now = datetime.now()
            for topic in topics:
                db_topic = DbNewStudioTopic(id_=topic.id, forum_id=forum_id, title=topic.title,
                                            download_id=topic.download_id, updated_at=now)
                session.add(db_topic)

            session.commit()

    @staticmethod
    def get_forum_topics(forum_id: int, session: OrmSession) -> Set[NewStudioTopic]:
        topics = set()

        db_topics = session.query(DbNewStudioTopic).filter(DbNewStudioTopic.forum_id == forum_id)
        for db_topic in db_topics:
            topic = NewStudioTopic(id_=db_topic.id, title=db_topic.title, download_id=db_topic.download_id)
            topics.add(topic)

        return topics


class NewStudio(object):
    @staticmethod
    def add_url_params(url: Text, params: dict) -> Text:
        url_parts = list(urlparse(url))
        query = dict(parse_qsl(url_parts[4]))
        query.update(params)
        url_parts[4] = urlencode(query)

        return urlunparse(url_parts)

    @staticmethod
    def add_timestamp(url: Text) -> Text:
        return NewStudio.add_url_params(url, {'__ts': int(time())})

    @staticmethod
    def get_forum_url(forum_id: int) -> Text:
        return '{0}/viewforum.php?f={1}'.format(BASE_URL, forum_id)

    @staticmethod
    def get_topic_url(topic_id: int) -> Text:
        return '{0}/viewtopic.php?t={1}'.format(BASE_URL, topic_id)

    @staticmethod
    def get_download_url(download_id: int) -> Text:
        return '{0}/download.php?id={1}'.format(BASE_URL, download_id)

    @staticmethod
    def get_forums(requests: RequestsSession) -> Set[NewStudioForum]:
        response = requests.get(BASE_URL)
        response.raise_for_status()
        return NewStudioParser.parse_forums(response.text)

    @staticmethod
    def get_forum_topics(forum_id: int, requests: RequestsSession) -> Set[NewStudioTopic]:
        items_count = 50
        result = set()
        pages_count = 0
        page_index = 0
        while True:
            url = NewStudio.get_forum_url(forum_id)
            start = page_index * items_count
            if start > 0:
                url = NewStudio.add_url_params(url, {'start': start})
            url = NewStudio.add_timestamp(url)
            response = requests.get(url)
            response.raise_for_status()
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


FORUMS_CACHE_DAYS_LIFETIME = 3
FORUM_TOPICS_CACHE_DAYS_LIFETIME = 1
SEARCH_STRING_REGEXPS = [
    re.compile(r'^(.*?)\s*(\d+?)x(\d+?)$', flags=re.IGNORECASE),
    re.compile(r'^(.*?)\s*s(\d+?)e(\d+?)$', flags=re.IGNORECASE)
]


class NewStudioPlugin(object):
    def url_rewritable(self, task: Task, entry: Entry) -> bool:
        topic_url = entry['url']
        match = TOPIC_ID_REGEXP.search(topic_url)
        if match:
            return True

        return False

    def url_rewrite(self, task: Task, entry: Entry) -> bool:
        topic_url = entry['url']
        topic_url = NewStudio.add_timestamp(topic_url)

        try:
            topic_response = task.requests.get(topic_url)
            topic_response.raise_for_status()
        except RequestException as e:
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

    def _search_forum(self, task: Task, title: Text, session: OrmSession) -> NewStudioForum:
        update_required = True
        db_timestamp = NewStudioDatabase.forums_timestamp(session)
        if db_timestamp:
            difference = datetime.now() - db_timestamp
            update_required = difference.days > FORUMS_CACHE_DAYS_LIFETIME
        if update_required:
            log.debug('Update forums...')
            forums = NewStudio.get_forums(task.requests)
            if forums:
                log.debug('{0} forum(s) received'.format(len(forums)))
                NewStudioDatabase.update_forums(forums, session)

        return NewStudioDatabase.find_forum_by_title(title, session)

    def _search_forum_topics(self, task: Task, forum_id: int, session: OrmSession) -> Set[NewStudioTopic]:
        update_required = True
        db_timestamp = NewStudioDatabase.forum_topics_timestamp(forum_id, session)
        if db_timestamp:
            difference = datetime.now() - db_timestamp
            update_required = difference.days > FORUM_TOPICS_CACHE_DAYS_LIFETIME
        if update_required:
            log.debug('Update topics for forum `{0}`...'.format(forum_id))
            topics = NewStudio.get_forum_topics(forum_id, task.requests)
            if topics:
                log.debug('{0} topic(s) received for forum `{1}`'.format(len(topics), forum_id))
                NewStudioDatabase.update_forum_topics(forum_id, topics, session)
                return topics

        return NewStudioDatabase.get_forum_topics(forum_id, session)

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

                forum = self._search_forum(task, search_title, session)
                if not forum:
                    log.debug("Unknown forum: {0} s{1:02d}e{2:02d}".format(search_title, search_season, search_episode))
                    continue

                try:
                    topics = self._search_forum_topics(task, forum.id, session)
                except Exception as error:
                    log.error("Error while getting topics of forum `Id={0}`:\n{1}".format(forum.id, error))
                    continue

                for topic in topics:
                    try:
                        topic_info = NewStudioParser.parse_topic_title(topic.title)
                    except ParsingError as e:
                        log.warning(e)
                    else:
                        if topic_info.season != search_season or not topic_info.contains_episode(search_episode):
                            continue

                        episode_id = topic_info.get_episode_id()

                        entry = Entry()
                        entry['title'] = "{0} / {1} / {2}".format(search_title, episode_id, topic_info.quality)
                        entry['url'] = NewStudio.get_download_url(topic.download_id)
                        # entry['series_season'] = topic_info.season
                        # entry['series_episode'] = topic_info.begin_episode
                        entry['series_id'] = episode_id
                        # entry['series_name'] = topic_info.title
                        # entry['quality'] = topic_info.quality

                        entries.add(entry)

            return entries


# endregion


@event('plugin.register')
def register_plugin() -> None:
    plugin.register(NewStudioAuthPlugin, PLUGIN_NAME + '_auth', api_ver=2)
    plugin.register(NewStudioPlugin, PLUGIN_NAME, interfaces=['urlrewriter', 'search'], api_ver=2)
