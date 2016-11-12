# -*- coding: utf-8 -*-
from __future__ import unicode_literals, division, absolute_import
from builtins import *  # pylint: disable=unused-import, redefined-builtin

import re
import json
import urllib
import logging
from time import sleep
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

from sqlalchemy import Column, Unicode, Integer, DateTime
from sqlalchemy.types import TypeDecorator, VARCHAR

from requests.auth import AuthBase
# from requests.utils import dict_from_cookiejar

from flexget import plugin
from flexget.entry import Entry
from flexget.event import event
from flexget.utils import requests
from flexget.manager import Session
from flexget.db_schema import versioned_base
from flexget.plugin import PluginError


Base = versioned_base('baibako_auth', 0)

log = logging.getLogger('baibako')

host_prefix = 'http://baibako.tv/'
host_regexp = re.compile(r'^https?://(?:www\.)?baibako\.tv.*$', flags=re.IGNORECASE)

details_url_regexp = re.compile(r'^https?://(?:www\.)?baibako\.tv/details\.php\?id=(\d+).*$', flags=re.IGNORECASE)

table_class_regexp = re.compile(r'table.*', flags=re.IGNORECASE)

episode_title_regexp = re.compile(
    r'^([^/]*?)\s*/\s*([^/]*?)\s*/\s*s(\d+)e(\d+)(?:-(\d+))?\s*/\s*([^/]*?)\s*(?:(?:/.*)|$)',
    flags=re.IGNORECASE)


class JSONEncodedDict(TypeDecorator):
    """Represents an immutable structure as a json-encoded string.

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
    """Supports downloading of torrents from 'baibako' tracker
           if you pass cookies (CookieJar) to constructor then authentication will be bypassed and cookies will be just set
        """

    def try_authenticate(self, payload):
        for _ in range(5):
            session = requests.Session()
            session.post('http://baibako.tv/takelogin.php', data=payload)
            baibako_cookies = session.cookies.get_dict(domain='baibako.tv')
            if baibako_cookies and len(baibako_cookies) > 0 and 'uid' in baibako_cookies:
                return baibako_cookies
            else:
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

    def __call__(self, r):
        r.prepare_cookies(self.cookies_)
        return r


class BaibakoShow(object):
    titles = []
    url = ''

    def __init__(self, titles, url):
        self.titles = titles
        self.url = url


class BaibakoUrlRewrite(object):
    """
    BaibaKo urlrewriter.

    Example::

      baibako:
        username: 'username_here'
        password: 'password_here'
        serial_tab: 'hd720'
    """

    schema = {
        'type': 'object',
        'properties': {
            'username': {'type': 'string'},
            'password': {'type': 'string'},
            'serial_tab': {'type': 'string'}
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

    def add_host_if_need(self, url):
        if not host_regexp.match(url):
            url = urllib.parse.urljoin(host_prefix, url)
        return url

    def url_rewritable(self, task, entry):
        url = entry['url']
        return details_url_regexp.match(url)

    def url_rewrite(self, task, entry):
        url = entry['url']
        url_match = details_url_regexp.search(url)
        if not url_match:
            reject_reason = "Url don't matched: {0}".format(url)
            log.verbose(reject_reason)
            # entry.reject(reject_reason)
            return False

        topic_id = url_match.group(1)
        url = 'http://baibako.tv/download.php?id={0}'.format(topic_id)
        entry['url'] = url
        return True

    @plugin.priority(127)
    def on_task_urlrewrite(self, task, config):
        auth_handler = self.get_auth_handler(config)
        for entry in task.accepted:
            entry['download_auth'] = auth_handler

    def search(self, task, entry, config=None):

        auth_handler = self.get_auth_handler(config)

        entries = set()

        serials_url = 'http://baibako.tv/serials.php'

        log.debug("Fetching serials page `{0}`...".format(serials_url))

        try:
            serials_response = requests.get(serials_url, auth=auth_handler)
        except requests.RequestException as e:
            log.error("Error while fetching page: {0}".format(e))
            return None
        serials_html = serials_response.text

        shows = set()

        serials_tree = BeautifulSoup(serials_html, 'html.parser')
        serials_table_node = serials_tree.find('table', class_=table_class_regexp)
        if not serials_table_node:
            log.error('Error while parsing serials page: node <table class=`table.*`> are not found')
            return None

        serial_link_nodes = serials_table_node.find_all('a')
        for serial_link_node in serial_link_nodes:
            serial_title = serial_link_node.text
            serial_link = serial_link_node.get('href')
            serial_link = self.add_host_if_need(serial_link)

            show = BaibakoShow([serial_title], serial_link)
            shows.add(show)

        log.debug("{0:d} show(s) are found".format(len(shows)))

        serial_tab = config.get('serial_tab', 'all')

        search_string_regexp = re.compile(r'^(.*?)\s*s(\d+)e(\d+)$', flags=re.IGNORECASE)
        episode_link_regexp = re.compile(r'details.php\?id=(\d+)', flags=re.IGNORECASE)

        for search_string in entry.get('search_strings', [entry['title']]):
            search_match = search_string_regexp.search(search_string)
            if not search_match:
                continue

            search_title = search_match.group(1)
            search_season = int(search_match.group(2))
            search_episode = int(search_match.group(3))

            for show in shows:
                if search_title not in show.titles:
                    continue

                serial_url = show.url + '&tab=' + serial_tab
                try:
                    serial_response = requests.get(serial_url, auth=auth_handler)
                except requests.RequestException as e:
                    log.error("Error while fetching page: {0}".format(e))
                    continue
                serial_html = serial_response.text

                serial_tree = BeautifulSoup(serial_html, 'html.parser')
                serial_table_node = serial_tree.find('table', class_=table_class_regexp)
                if not serial_table_node:
                    log.error('Error while parsing serial page: node <table class=`table.*`> are not found')
                    continue

                link_nodes = serial_table_node.find_all('a', href=episode_link_regexp)
                for link_node in link_nodes:
                    link_title = link_node.text
                    episode_title_match = episode_title_regexp.search(link_title)
                    if not episode_title_match:
                        log.verbose("Error while parsing serial page: title `{0}` are not matched".format(link_title))
                        continue

                    season = int(episode_title_match.group(3))
                    first_episode = int(episode_title_match.group(4))
                    last_episode = first_episode
                    last_episode_group = episode_title_match.group(5)
                    if last_episode_group:
                        last_episode = int(last_episode_group)

                    if season != search_season or (first_episode > search_episode or last_episode < search_episode):
                        continue

                    ru_title = episode_title_match.group(1)
                    title = episode_title_match.group(2)
                    quality = episode_title_match.group(6)

                    if last_episode > first_episode:
                        episode_id = 's{0:02d}e{1:02d}-e{2:02d}'.format(season, first_episode, last_episode)
                    else:
                        episode_id = 's{0:02d}e{1:02d}'.format(season, first_episode)

                    entry_title = "{0} / {1} / {2} / {3}".format(title, ru_title, episode_id, quality)
                    entry_url = link_node.get('href')
                    entry_url = self.add_host_if_need(entry_url)

                    entry = Entry()
                    entry['title'] = entry_title
                    entry['url'] = entry_url

                    entries.add(entry)

        return entries


@event('plugin.register')
def register_plugin():
    plugin.register(BaibakoUrlRewrite, 'baibako', groups=['urlrewriter', 'search'], api_ver=2)
