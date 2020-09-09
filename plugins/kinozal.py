# -*- coding: utf-8 -*-

import logging
import re
from datetime import datetime, timedelta
from time import sleep
from typing import Optional, Set, Text, Dict
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from flexget import plugin
from flexget.components.sites import utils
from flexget.db_schema import versioned_base
from flexget.entry import Entry
from flexget.event import event
from flexget.manager import Session
from flexget.plugin import PluginError
from requests import Session as RequestsSession, PreparedRequest, RequestException
from requests.auth import AuthBase
from sqlalchemy import Column, Unicode, Integer, DateTime
from sqlalchemy.orm import Session as OrmSession

from .utils import JSONEncodedDict

PLUGIN_NAME = 'kinozal'
SCHEMA_VER = 0

log = logging.getLogger(PLUGIN_NAME)
Base = versioned_base(PLUGIN_NAME, SCHEMA_VER)

BASE_URL = 'http://kinozal.tv'
COOKIES_DOMAIN = '.kinozal.tv'

HOST_REGEXP = re.compile(r'^https?://(?:www\.)?(?:.+\.)?kinozal\.tv', flags=re.IGNORECASE)


def validate_host(url: Text) -> bool:
    return HOST_REGEXP.match(url) is not None


# region KinozalAuthPlugin
class KinozalAccount(Base):
    __tablename__ = 'kinozal_accounts'
    id = Column(Integer, primary_key=True, autoincrement=True, nullable=False)
    username = Column(Unicode, index=True, nullable=False, unique=True)
    cookies = Column(JSONEncodedDict)
    expiry_time = Column(DateTime, nullable=False)

    def __init__(self, username: str, cookies: dict, expiry_time: datetime) -> None:
        self.username = username
        self.cookies = cookies
        self.expiry_time = expiry_time


class KinozalAuth(AuthBase):
    def try_authenticate(self, payload):
        for _ in range(5):
            with RequestsSession() as session:
                response = session.post('{0}/takelogin.php'.format(BASE_URL), data=payload)
                response.raise_for_status()

                cookies = session.cookies.get_dict(domain=COOKIES_DOMAIN)
                if cookies and len(cookies) > 0:
                    return cookies

            sleep(3)

        raise PluginError('Unable to obtain cookies from Kinozal. Looks like invalid username or password.')

    def __init__(self, username: Text, password: Text, cookies: Dict = None, session: OrmSession = None) -> None:
        if cookies is None:
            log.debug('Kinozal cookie not found. Requesting new one.')
            payload_ = {'username': username, 'password': password}
            self.__cookies = self.try_authenticate(payload_)
            if session:
                session.add(
                    KinozalAccount(
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


class KinozalAuthPlugin(object):
    """Usage:

    kinozal_auth:
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

    def try_find_cookie(self, session: OrmSession, username: Text) -> Optional[Dict]:
        account = session.query(KinozalAccount).filter(KinozalAccount.username == username).first()
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
                auth_handler = KinozalAuth(username, password, cookies, session)
                self.auth_cache[username] = auth_handler
            else:
                auth_handler = self.auth_cache[username]

        return auth_handler

    @plugin.priority(plugin.PRIORITY_DEFAULT)
    def on_task_start(self, task, config):
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
            log.debug('setting auth with username %s for %s', username, entry)
            entry['download_auth'] = self.get_auth_handler(config)


# endregion


# region KinozalPlugin
DEFAULT_CATEGORY = 0
CATEGORIES = {
    'all': DEFAULT_CATEGORY,
    'serials': 1001,
    'movies': 1002,
    'cartoons': 1003
}

DEFAULT_QUALITY = 0
QUALITIES = {
    'all': DEFAULT_QUALITY,
    'hdrip': 1,  # DVD / BD(HD)   HDRip, BDRip
    'dvd': 2,  # dvd-5 / dvd-9,
    'hd': 3,  # hd 720p 1080p
    'remux': 4,  # Blu-Ray and Remux
    'tvrip': 5,  # TV Rips   HDTVRip
    '3d': 6  # 3D BDRip and other
}

DEFAULT_FILTER = 0
FILTER = {
    'none': DEFAULT_FILTER,
    'today': 1,
    'yesterday': 2,
    '3days': 3,
    'week': 4,
    'month': 5,
    '<1.3gb': 6,
    '1.3-2.2gb': 7,
    '2.2-4.0gb': 8,
    '4.0-9.5gb': 9,
    '>9.5gb': 10,
    'gold': 11,
    'silver': 12
}

DEFAULT_SORT = 0
SORT = {
    'default': DEFAULT_SORT,
    'date': 0,
    'seeds': 1,
    'leechers': 2,
    'size': 3,
    'comments': 4,
    'downloads': 5,
    'last_comment': 6
}

DEFAULT_SORT_ORDER = 0
SORT_ORDER = {
    'default': DEFAULT_SORT_ORDER,
    'desc': 0,
    'asc': 1
}

DETAILS_URL_REGEXP = re.compile(r'^https?://(?:www\.)?kinozal\.tv/details\.php\?id=(\d+).*$', flags=re.IGNORECASE)
INFO_HASH_REGEXP = re.compile(r'^.*\s+(\w+)$', flags=re.IGNORECASE)


class KinozalSearchEntry(object):
    def __init__(self, id_: int, title: Text, url: Text) -> None:
        self.id = id_
        self.title = title
        self.url = url
        self.comments = 0
        self.size = 0
        self.seeds = 0
        self.leeches = 0
        self.date = None
        self.release = None


class KinozalParser(object):
    @staticmethod
    def parse_filesize(text_size: Text) -> int:
        prefix_order = {'': 0, 'к': 1, 'м': 2, 'г': 3, 'т': 4, 'п': 5}
        parsed_size = re.match(
            r'(\d+(?:[.,\s]\d+)*)(?:\s*)((?:[птгмк])?б)', text_size.strip().lower(), flags=re.UNICODE
        )
        if not parsed_size:
            raise ValueError('%s does not look like a file size' % text_size)
        amount = parsed_size.group(1)
        unit = parsed_size.group(2)
        if not unit.endswith('б'):
            raise ValueError('%s does not look like a file size' % text_size)
        unit = unit.rstrip('б')
        if unit not in prefix_order:
            raise ValueError('%s does not look like a file size' % text_size)
        order = prefix_order[unit]
        amount = float(amount.replace(',', '').replace(' ', ''))
        return (amount * (1024 ** order)) / 1024 ** 2

    @staticmethod
    def parse_topic_id(url: Text) -> Optional[int]:
        url_match = DETAILS_URL_REGEXP.search(url)
        if not url_match:
            return None
        return int(url_match.group(1))

    @staticmethod
    def parse_info_hash(html: Text) -> Optional[Text]:
        soup = BeautifulSoup(html, 'html.parser')
        hash_node = soup.find('li')
        if not hash_node:
            return None
        hash_node_text = hash_node.text
        match = INFO_HASH_REGEXP.search(hash_node_text)
        if not match:
            return None
        info_hash = match.group(1)
        return info_hash.lower()

    @staticmethod
    def parse_search_result(html: Text, base_url: Text) -> Optional[Set[KinozalSearchEntry]]:
        entries = set()

        table_class_regexp = re.compile(r'^t_peer.*$', flags=re.IGNORECASE)
        row_class_regexp = re.compile(r'^.*bg$', flags=re.IGNORECASE)

        soup = BeautifulSoup(html, 'html.parser')
        table_node = soup.find('table', class_=table_class_regexp)
        if table_node:
            row_nodes = table_node.find_all('tr', class_=row_class_regexp)
            for row_node in row_nodes:
                column_node = row_node.find('td', class_='nam')
                if column_node:
                    link_node = column_node.find('a')
                    if link_node:
                        title = link_node.text
                        url = link_node.get('href')
                        url = urljoin(base_url, url)
                        url_match = DETAILS_URL_REGEXP.search(url)
                        if not url_match:
                            continue

                        topic_id = int(url_match.group(1))

                        entry = KinozalSearchEntry(topic_id, title, url)
                        column_nodes = column_node.find_all('td')
                        if len(column_nodes) >= 6:
                            entry.comments = int(column_nodes[0].text)
                            entry.size = KinozalParser.parse_filesize(column_nodes[1].text)
                            entry.seeds = int(column_nodes[2].text)
                            entry.leeches = int(column_nodes[3].text)
                            entry.date = column_nodes[4].text
                            entry.release = column_nodes[5].text

                        entries.add(entry)

        return entries


class Kinozal(object):
    @staticmethod
    def get_info_hash(requests: RequestsSession, topic_id: int) -> Optional[Text]:
        response = requests.get('{0}/get_srv_details.php?id={1}&action=2'.format(BASE_URL, topic_id))
        response.raise_for_status()
        return KinozalParser.parse_info_hash(response.text)

    @staticmethod
    def search(requests: RequestsSession, search_string, page=0,
               category=DEFAULT_CATEGORY, quality=DEFAULT_QUALITY,
               filter_=DEFAULT_FILTER, sort_by=DEFAULT_SORT,
               sort_order=DEFAULT_SORT_ORDER) -> Optional[Set[KinozalSearchEntry]]:
        payload = {
            'page': page,
            's': search_string,
            'g': 0,  # where (0 - default)
            'c': category,
            'v': quality,
            'd': 0,  # year (0 - default)
            'w': filter_,
            't': sort_by,
            'f': sort_order
        }

        response = requests.get('{0}/browse.php'.format(BASE_URL), params=payload)
        response.raise_for_status()

        return KinozalParser.parse_search_result(response.text, response.url)


class KinozalPlugin(object):
    """Kinozal urlrewriter/search plugin."""

    schema = {
        'oneOf': [
            {'type': 'boolean'},
            {
                'type': 'object',
                'properties': {
                    'category': {
                        'oneOf': [
                            {'type': 'string', 'enum': list(CATEGORIES)},
                            {'type': 'integer'}
                        ]
                    },
                    'quality': {
                        'oneOf': [
                            {'type': 'string', 'enum': list(QUALITIES)},
                            {'type': 'integer'}
                        ]
                    },
                    'filter': {
                        'oneOf': [
                            {'type': 'string', 'enum': list(FILTER)},
                            {'type': 'integer'}
                        ]
                    },
                    'sort_by': {
                        'oneOf': [
                            {'type': 'string', 'enum': list(SORT)},
                            {'type': 'integer'}
                        ]
                    },
                    'sort_order': {
                        'oneOf': [
                            {'type': 'string', 'enum': list(SORT_ORDER)},
                            {'type': 'integer'}
                        ]
                    },
                },
                'additionalProperties': False
            }
        ]
    }

    def url_rewritable(self, task, entry):
        return KinozalParser.parse_topic_id(entry['url']) is not None

    def url_rewrite(self, task, entry):
        url = entry['url']
        topic_id = KinozalParser.parse_topic_id(url)
        if not topic_id:
            log.warning("Url don't matched: {0}".format(url))
            return False

        url = '{0}/download.php?id={1}'.format(BASE_URL, topic_id)
        entry['url'] = url
        return True

    @plugin.priority(plugin.PRIORITY_LAST)
    def on_task_filter(self, task, config):
        if not config:
            log.debug('Filter disabled, skipping')
            return
        for entry in task.entries:
            url = entry['url']
            topic_id = KinozalParser.parse_topic_id(url)
            if not topic_id:
                log.debug('Invalid url `{0}`, skipping'.format(url))
                continue
            if 'torrent_info_hash' not in entry:
                log.debug('Entry {0} has no torrent_info_hash, skipping'.format(entry))
                continue
            torrent_info_hash = entry['torrent_info_hash'].lower()
            info_hash = Kinozal.get_info_hash(task.requests, topic_id)
            log.debug('Equals hash info {0} with {1}...'.format(torrent_info_hash, info_hash))
            if torrent_info_hash == info_hash:
                entry.reject('Already up-to-date torrent with this infohash')
                continue

            entry['torrent_info_hash'] = info_hash
            entry.accept()

    def search(self, task, entry, config=None):
        if not isinstance(config, dict):
            config = {}

        category = config.get('category', DEFAULT_CATEGORY)
        if not isinstance(config, int):
            category = CATEGORIES.get(category, DEFAULT_CATEGORY)

        quality = config.get('quality', DEFAULT_QUALITY)
        if not isinstance(quality, int):
            quality = QUALITIES.get(quality, DEFAULT_QUALITY)

        filter = config.get('filter', DEFAULT_FILTER)
        if not isinstance(filter, int):
            filter = FILTER.get(filter, DEFAULT_FILTER)

        sort_by = config.get('sort_by', DEFAULT_SORT)
        if not isinstance(sort_by, int):
            sort_by = SORT.get(sort_by, DEFAULT_SORT)

        sort_order = config.get('sort_order', DEFAULT_SORT_ORDER)
        if not isinstance(sort_order, int):
            sort_order = SORT_ORDER.get(sort_order, DEFAULT_SORT_ORDER)

        entries = set()
        for search_string in entry.get('search_strings', [entry['title']]):
            try:
                search_result = Kinozal.search(task.requests, search_string,
                                               category=category, quality=quality,
                                               filter_=filter, sort_by=sort_by,
                                               sort_order=sort_order)
            except RequestException as e:
                log.error("Error while fetching page: {0}".format(e))
                sleep(3)
                continue
            sleep(3)

            for search_entry in search_result:
                entry = Entry()
                entry['title'] = search_entry.title
                entry['url'] = search_entry.url
                entry['torrent_seeds'] = search_entry.seeds
                entry['torrent_leeches'] = search_entry.leeches
                entry['content_size'] = search_entry.size
                entry['search_sort'] = utils.torrent_availability(search_entry.seeds, search_entry.leeches)

                # info_hash = Kinozal.get_info_hash(task.requests, search_entry.id)
                # if info_hash:
                #     entry['torrent_info_hash'] = info_hash

                entries.add(entry)

        return entries


# endregion


@event('plugin.register')
def register_plugin():
    plugin.register(KinozalAuthPlugin, PLUGIN_NAME + '_auth', api_ver=2)
    plugin.register(KinozalPlugin, PLUGIN_NAME, interfaces=['urlrewriter', 'search', 'task'], api_ver=2)
