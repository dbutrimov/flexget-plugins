# -*- coding: utf-8 -*-

import json
import logging
import re
from datetime import datetime, timedelta
from time import sleep
from typing import Text, Dict, Optional, List, Set
from urllib.parse import urljoin

import requests
import sqlalchemy.orm
from bs4 import BeautifulSoup
from flexget import plugin
from flexget.db_schema import versioned_base
from flexget.entry import Entry
from flexget.event import event
from flexget.manager import Session
from flexget.plugin import PluginError
from flexget.task import Task
from requests.auth import AuthBase
from sqlalchemy import Column, Unicode, Integer, DateTime, UniqueConstraint, ForeignKey, func
from sqlalchemy.types import TypeDecorator, VARCHAR

PLUGIN_NAME = 'alexfilm'
SCHEMA_VER = 0

log = logging.getLogger(PLUGIN_NAME)
Base = versioned_base(PLUGIN_NAME, SCHEMA_VER)

BASE_URL = 'http://alexfilm.org'
COOKIES_DOMAIN = '.alexfilm.org'

HOST_REGEXP = re.compile(r'^https?://(?:www\.)?(?:.+\.)?alexfilm\.org', flags=re.IGNORECASE)


def process_url(url: Text, base_url: Text) -> Text:
    return urljoin(base_url, url)


def validate_host(url: Text) -> bool:
    return HOST_REGEXP.match(url) is not None


# region AlexFilmAuthPlugin
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


class AlexFilmAccount(Base):
    __tablename__ = 'alexfilm_accounts'
    id = Column(Integer, primary_key=True, autoincrement=True, nullable=False)
    username = Column(Unicode, index=True, nullable=False, unique=True)
    cookies = Column(JSONEncodedDict)
    expiry_time = Column(DateTime, nullable=False)

    def __init__(self, username: str, cookies: dict, expiry_time: datetime) -> None:
        self.username = username
        self.cookies = cookies
        self.expiry_time = expiry_time


class AlexFilmAuth(AuthBase):
    """
    Supports downloading of torrents from AlexFilm tracker
    if you pass cookies (CookieJar) to constructor then authentication will be bypassed
    and cookies will be just set.
    """

    def try_authenticate(self, payload: Dict) -> Dict:
        for _ in range(5):
            session = requests.Session()
            try:
                response = session.post('{0}/login.php'.format(BASE_URL), data=payload)
                response.raise_for_status()

                cookies = session.cookies.get_dict(domain=COOKIES_DOMAIN)
                if cookies and len(cookies) > 0:
                    return cookies
            finally:
                session.close()
            sleep(3)

        raise PluginError('Unable to obtain cookies from AlexFilm. Looks like invalid username or password.')

    def __init__(self, username: Text, password: Text,
                 cookies: Dict = None, db_session: sqlalchemy.orm.Session = None) -> None:
        if cookies is None:
            log.debug('AlexFilm cookie not found. Requesting new one.')

            payload_ = {
                'login_username': username,
                'login_password': password,
                'login': "Вход",
                'autologin': 1
            }

            self.__cookies = self.try_authenticate(payload_)
            if db_session:
                db_session.add(
                    AlexFilmAccount(
                        username=username,
                        cookies=self.__cookies,
                        expiry_time=datetime.now() + timedelta(days=1)))
                db_session.commit()
            # else:
            #     raise ValueError(
            #         'db_session can not be None if cookies is None')
        else:
            log.debug('Using previously saved cookie.')
            self.__cookies = cookies

    def __call__(self, request: requests.PreparedRequest) -> requests.PreparedRequest:
        # request.prepare_cookies(self.__cookies)
        if validate_host(request.url):
            request.headers['Cookie'] = '; '.join('{0}={1}'.format(key, val) for key, val in self.__cookies.items())
        return request


class AlexFilmAuthPlugin(object):
    """Usage:

    alexfilm_auth:
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

    def try_find_cookie(self, db_session: sqlalchemy.orm.Session, username: Text) -> Optional[Dict]:
        account = db_session.query(AlexFilmAccount).filter(AlexFilmAccount.username == username).first()
        if account:
            if account.expiry_time < datetime.now():
                db_session.delete(account)
                db_session.commit()
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

        db_session = Session()
        cookies = self.try_find_cookie(db_session, username)
        if username not in self.auth_cache:
            auth_handler = AlexFilmAuth(username, password, cookies, db_session)
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


# region AlexFilmPlugin
class DbAlexFilmShow(Base):
    __tablename__ = 'alexfilm_shows'
    id = Column(Integer, primary_key=True, nullable=False)
    title = Column(Unicode, index=True, nullable=False)
    url = Column(Unicode, nullable=False)
    updated_at = Column(DateTime, nullable=False)

    def __init__(self, id_: int, title: str, url: str, updated_at: datetime) -> None:
        self.id = id_
        self.title = title
        self.url = url
        self.updated_at = updated_at


class DbAlexFilmShowAlternateName(Base):
    __tablename__ = 'alexfilm_show_alternate_names'
    id = Column(Integer, primary_key=True, autoincrement=True, nullable=False)
    show_id = Column(Integer, ForeignKey('alexfilm_shows.id'), nullable=False)
    title = Column(Unicode, index=True, nullable=False)
    __table_args__ = (UniqueConstraint('show_id', 'title', name='_show_title_uc'),)

    def __init__(self, show_id: int, title: str) -> None:
        self.show_id = show_id
        self.title = title


class AlexFilmShow(object):
    def __init__(self, show_id: int, titles: List[Text], url: Text) -> None:
        self.show_id = show_id
        self.titles = titles
        self.url = url


class ParsingError(Exception):
    def __init__(self, message: Text) -> None:
        self.message = message

    def __str__(self):
        return "{0}".format(self.message)

    def __unicode__(self):
        return u"{0}".format(self.message)


class AlexFilmParser(object):
    @staticmethod
    def parse_download_url(html: Text) -> Text:
        bs = BeautifulSoup(html, 'html.parser')
        download_node = bs.find('a', href=DOWNLOAD_URL_REGEXP)
        if not download_node:
            raise ParsingError('download node is not found')

        return download_node.get('href')

    @staticmethod
    def parse_download_id(html: Text) -> int:
        url = AlexFilmParser.parse_download_url(html)
        match = DOWNLOAD_URL_REGEXP.search(url)
        if not match:
            raise ParsingError('invalid download url format')

        return int(match.group(1))

    @staticmethod
    def parse_magnet(html: Text) -> Text:
        bs = BeautifulSoup(html, 'html.parser')
        magnet_node = bs.find('a', id='magnet')
        if not magnet_node:
            raise ParsingError('magnet node is not found')

        return magnet_node.get('href')

    @staticmethod
    def parse_shows_page(html: Text) -> Optional[Set[AlexFilmShow]]:
        serials_tree = BeautifulSoup(html, 'html.parser')
        serials_node = serials_tree.find('ul', id='serials')
        if not serials_node:
            log.error('Error while parsing serials page: node <ul id=`serials`> are not found')
            return None

        shows = set()

        url_regexp = re.compile(r'f=(\d+)', flags=re.IGNORECASE)
        url_nodes = serials_node.find_all('a', href=url_regexp)
        for url_node in url_nodes:
            href = url_node.get('href')
            url_match = url_regexp.search(href)
            if not url_match:
                continue

            show_id = int(url_match.group(1))
            titles = url_node.text.split(' / ')
            if len(titles) > 0:
                show = AlexFilmShow(show_id=show_id, titles=titles, url=href)
                shows.add(show)

        return shows


class AlexFilmDatabase(object):
    @staticmethod
    def shows_timestamp(db_session: sqlalchemy.orm.Session) -> datetime:
        return db_session.query(func.min(DbAlexFilmShow.updated_at)).scalar() or None

    @staticmethod
    def shows_count(db_session: sqlalchemy.orm.Session) -> int:
        return db_session.query(DbAlexFilmShow).count()

    @staticmethod
    def clear_shows(db_session: sqlalchemy.orm.Session) -> None:
        db_session.query(DbAlexFilmShowAlternateName).delete()
        db_session.query(DbAlexFilmShow).delete()
        db_session.commit()

    @staticmethod
    def update_shows(shows: Set[AlexFilmShow], db_session: sqlalchemy.orm.Session) -> None:
        # Clear database
        AlexFilmDatabase.clear_shows(db_session)

        # Insert new rows
        if shows and len(shows) > 0:
            now = datetime.now()
            for show in shows:
                db_show = DbAlexFilmShow(id_=show.show_id, title=show.titles[0], url=show.url, updated_at=now)
                db_session.add(db_show)

                for index, item in enumerate(show.titles[1:], start=1):
                    alternate_name = DbAlexFilmShowAlternateName(show_id=show.show_id, title=item)
                    db_session.add(alternate_name)

            db_session.commit()

    @staticmethod
    def get_shows(db_session: sqlalchemy.orm.Session) -> Set[AlexFilmShow]:
        shows = set()

        db_shows = db_session.query(DbAlexFilmShow).all()
        for db_show in db_shows:
            titles = list()
            titles.append(db_show.title)

            db_alternate_names = db_session.query(DbAlexFilmShowAlternateName).filter(
                DbAlexFilmShowAlternateName.show_id == db_show.id).all()
            if db_alternate_names and len(db_alternate_names) > 0:
                for db_alternate_name in db_alternate_names:
                    titles.append(db_alternate_name.title)

            show = AlexFilmShow(show_id=db_show.id, titles=titles, url=db_show.url)
            shows.add(show)

        return shows

    @staticmethod
    def get_show_by_id(show_id: int, db_session: sqlalchemy.orm.Session) -> Optional[AlexFilmShow]:
        db_show = db_session.query(DbAlexFilmShow).filter(DbAlexFilmShow.id == show_id).first()
        if db_show:
            titles = list()
            titles.append(db_show.title)

            db_alternate_names = db_session.query(DbAlexFilmShowAlternateName).filter(
                DbAlexFilmShowAlternateName.show_id == db_show.id).all()
            if db_alternate_names and len(db_alternate_names) > 0:
                for db_alternate_name in db_alternate_names:
                    titles.append(db_alternate_name.title)

            show = AlexFilmShow(show_id=db_show.id, titles=titles, url=db_show.url)
            return show

        return None

    @staticmethod
    def find_show_by_title(title: Text, db_session: sqlalchemy.orm.Session) -> Optional[AlexFilmShow]:
        db_show = db_session.query(DbAlexFilmShow).filter(DbAlexFilmShow.title == title).first()
        if db_show:
            return AlexFilmDatabase.get_show_by_id(db_show.id, db_session)

        db_alternate_name = db_session.query(DbAlexFilmShowAlternateName).filter(
            DbAlexFilmShowAlternateName.title == title).first()
        if db_alternate_name:
            return AlexFilmDatabase.get_show_by_id(db_alternate_name.show_id, db_session)

        return None


TOPIC_URL_REGEXP = re.compile(r'^https?://(?:www\.)?alexfilm\.org/viewtopic\.php\?t=(\d+).*$', flags=re.IGNORECASE)
DOWNLOAD_URL_REGEXP = re.compile(r'dl\.php\?id=(\d+)', flags=re.IGNORECASE)
SEARCH_STRING_REGEXPS = [
    re.compile(r'^(.*?)\s*(\d+?)x(\d+?)$', flags=re.IGNORECASE),
    re.compile(r'^(.*?)\s*s(\d+?)e(\d+?)$', flags=re.IGNORECASE)
]


class AlexFilm(object):
    @staticmethod
    def get_topic_url(topic_id: int) -> Text:
        return '{0}/viewtopic.php?t={1}'.format(BASE_URL, topic_id)

    @staticmethod
    def get_download_id(requests_: requests.Session, topic_id: int) -> int:
        topic_url = AlexFilm.get_topic_url(topic_id)
        topic_response = requests_.get(topic_url)
        topic_response.raise_for_status()
        return AlexFilmParser.parse_download_id(topic_response.text)

    @staticmethod
    def get_download_url(requests_: requests.Session, topic_id: int) -> str:
        download_id = AlexFilm.get_download_id(requests_, topic_id)
        return '{0}/dl.php?id={1}'.format(BASE_URL, download_id)

    @staticmethod
    def get_marget(requests_: requests.Session, topic_id: int) -> Text:
        topic_url = AlexFilm.get_topic_url(topic_id)
        topic_response = requests_.get(topic_url)
        topic_response.raise_for_status()
        return AlexFilmParser.parse_magnet(topic_response.text)


class AlexFilmPlugin(object):
    """AlexFilm urlrewrite/search plugin."""

    def url_rewritable(self, task: Task, entry: Entry) -> bool:
        url = entry['url']
        match = TOPIC_URL_REGEXP.match(url)
        if match:
            return True

        return False

    def url_rewrite(self, task: Task, entry: Entry) -> bool:
        topic_url = entry['url']

        try:
            topic_response = task.requests.get(topic_url)
            topic_response.raise_for_status()
        except requests.RequestException as e:
            reject_reason = "Error while fetching page: {0}".format(e)
            log.error(reject_reason)
            entry.reject(reject_reason)
            sleep(3)
            return False
        topic_html = topic_response.content
        sleep(3)

        try:
            download_url = AlexFilmParser.parse_download_url(topic_html)
        except ParsingError as e:
            reject_reason = "Error while parsing topic page: {0}".format(e)
            log.error(reject_reason)
            entry.reject(reject_reason)
            return False

        download_url = process_url(download_url, topic_response.url)

        entry['url'] = download_url
        return True

    def get_shows(self, task: Task) -> Optional[Set[AlexFilmShow]]:
        try:
            serials_response = task.requests.get(BASE_URL)
            serials_response.raise_for_status()
        except requests.RequestException as e:
            log.error("Error while fetching page: {0}".format(e))
            sleep(3)
            return None
        serials_html = serials_response.text
        sleep(3)

        shows = AlexFilmParser.parse_shows_page(serials_html)
        if shows:
            for show in shows:
                show.url = process_url(show.url, serials_response.url)

        return shows

    def search_show(self, task: Task, title: Text, db_session: sqlalchemy.orm.Session) -> Optional[AlexFilmShow]:
        update_required = True
        db_timestamp = AlexFilmDatabase.shows_timestamp(db_session)
        if db_timestamp:
            difference = datetime.now() - db_timestamp
            update_required = difference.days > 3
        if update_required:
            log.debug('Update shows...')
            shows = self.get_shows(task)
            if shows:
                log.debug('{0} show(s) received'.format(len(shows)))
                AlexFilmDatabase.update_shows(shows, db_session)

        show = AlexFilmDatabase.find_show_by_title(title, db_session)
        return show

    def search(self, task: Task, entry: Entry, config: Dict = None) -> Set[Entry]:
        db_session = Session()

        topic_name_regexp = re.compile(
            r"^([^/]*?)\s*/\s*([^/]*?)\s/\s*[Сс]езон\s*(\d+)\s*/\s*[Сс]ерии\s*(\d+)-(\d+).*,\s*(.*)\s*\].*$",
            flags=re.IGNORECASE)
        panel_class_regexp = re.compile(r'panel.*', flags=re.IGNORECASE)
        url_regexp = re.compile(r'viewtopic\.php\?t=(\d+)', flags=re.IGNORECASE)

        # regexp: '^([^/]*?)\s*/\s*([^/]*?)\s/\s*[Сс]езон\s*(\d+)\s*/\s*[Сс]ерии\s*(\d+)-(\d+).*,\s*(.*)\s*\].*$'
        # format: '\2 / \1 / s\3e\4-e\5 / \6'

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

            show = self.search_show(task, search_title, db_session)
            if not show:
                log.warning("Unknown show: {0}".format(search_title))
                continue

            try:
                serial_response = task.requests.get(show.url)
                serial_response.raise_for_status()
            except requests.RequestException as e:
                log.error("Error while fetching page: {0}".format(e))
                sleep(3)
                continue
            serial_html = serial_response.text
            sleep(3)

            serial_tree = BeautifulSoup(serial_html, 'html.parser')
            serial_table_node = serial_tree.find('section')
            if not serial_table_node:
                log.error('Error while parsing serial page: node <table class=`table.*`> are not found')
                continue

            panel_nodes = serial_table_node.find_all('div', class_=panel_class_regexp)
            for panel_node in panel_nodes:
                url_node = panel_node.find('a', href=url_regexp)
                if not url_node:
                    continue

                topic_name = url_node.text
                name_match = topic_name_regexp.match(topic_name)
                if not name_match:
                    continue

                title = name_match.group(2)
                alternative_title = name_match.group(1)
                season = int(name_match.group(3))
                first_episode = int(name_match.group(4))
                last_episode = int(name_match.group(5))
                quality = name_match.group(6)

                if search_season != season or (search_episode < first_episode or search_episode > last_episode):
                    continue

                episode_id = "s{0:02d}e{1:02d}-{2:02d}".format(season, first_episode, last_episode)
                name = "{0} / {1} / {2} / {3}".format(title, alternative_title, episode_id, quality)
                topic_url = url_node.get('href')
                topic_url = process_url(topic_url, serial_response.url)

                log.debug("{0} - {1}".format(name, topic_url))

                entry = Entry()
                entry['title'] = name
                entry['url'] = topic_url
                entry['series_id'] = episode_id

                entries.add(entry)

        return entries


# endregion


@event('plugin.register')
def register_plugin() -> None:
    plugin.register(AlexFilmAuthPlugin, PLUGIN_NAME + '_auth', api_ver=2)
    plugin.register(AlexFilmPlugin, PLUGIN_NAME, interfaces=['urlrewriter', 'search'], api_ver=2)
