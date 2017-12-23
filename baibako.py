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

PLUGIN_NAME = 'baibako'
SCHEMA_VER = 0

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
    """Supports downloading of torrents from 'baibako' tracker
           if you pass cookies (CookieJar) to constructor then authentication will be bypassed and cookies will be just set
        """

    def try_authenticate(self, payload):
        for _ in range(5):
            session = requests.Session()
            session.post('http://baibako.tv/takelogin.php', data=payload)
            cookies = session.cookies.get_dict(domain='baibako.tv')
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
DETAILS_URL_REGEXP = re.compile(r'^https?://(?:www\.)?baibako\.tv/details\.php\?id=(\d+).*$', flags=re.IGNORECASE)

TABLE_CLASS_REGEXP = re.compile(r'table.*', flags=re.IGNORECASE)
EPISODE_TITLE_REGEXP = re.compile(
    r'^([^/]*?)\s*/\s*([^/]*?)\s*/\s*s(\d+)e(\d+)(?:-(\d+))?\s*/\s*([^/]*?)\s*(?:(?:/.*)|$)',
    flags=re.IGNORECASE)

EPISODE_LINK_REGEXP = re.compile(r'details.php\?id=(\d+)', flags=re.IGNORECASE)


class DbBaibakoShow(Base):
    __tablename__ = 'baibako_shows'
    id = Column(Integer, primary_key=True, nullable=False)
    title = Column(Unicode, index=True, nullable=False)
    url = Column(Unicode, nullable=False)
    updated_at = Column(DateTime, nullable=False)


class DbBaibakoShowAlternateName(Base):
    __tablename__ = 'baibako_show_alternate_names'
    id = Column(Integer, primary_key=True, autoincrement=True, nullable=False)
    show_id = Column(Integer, ForeignKey('baibako_shows.id'), nullable=False)
    title = Column(Unicode, index=True, nullable=False)
    __table_args__ = (UniqueConstraint('show_id', 'title', name='_show_title_uc'),)


class DbBaibakoEpisode(Base):
    __tablename__ = 'baibako_episode'
    id = Column(Integer, primary_key=True, autoincrement=True, nullable=False)
    show_id = Column(Integer, nullable=False)
    season = Column(Integer, nullable=False)
    episode = Column(Integer, nullable=False)
    title = Column(Unicode, nullable=False)
    url = Column(Unicode, nullable=False)
    timestamp = Column(DateTime, nullable=False)
    __table_args__ = (UniqueConstraint('show_id', 'season', 'episode', name='_show_episode_uc'),)


class BaibakoParser(object):
    @staticmethod
    def parse_shows_page(html):
        serials_tree = BeautifulSoup(html, 'html.parser')
        serials_node = serials_tree.find('table', class_=TABLE_CLASS_REGEXP)
        if not serials_node:
            raise Exception('Node <table class=`table.*`> are not found')

        shows = []

        url_regexp = re.compile(r'id=(\d+)', flags=re.IGNORECASE)
        link_nodes = serials_node.find_all('a')
        for link_node in link_nodes:
            serial_link = link_node.get('href')
            url_match = url_regexp.search(serial_link)
            if not url_match:
                continue

            show_id = int(url_match.group(1))
            serial_title = link_node.text

            shows.append({
                'show_id': show_id,
                'titles': [serial_title],
                'url': serial_link
            })

        log.debug("{0:d} show(s) are found".format(len(shows)))
        return shows

    @staticmethod
    def parse_episodes_page(html):
        serial_tree = BeautifulSoup(html, 'html.parser')
        serial_table_node = serial_tree.find('table', class_=TABLE_CLASS_REGEXP)
        if not serial_table_node:
            raise Exception('Node <table class=`table.*`> are not found')

        entries = list()

        link_nodes = serial_table_node.find_all('a', href=EPISODE_LINK_REGEXP)
        for link_node in link_nodes:
            link_title = link_node.text
            episode_title_match = EPISODE_TITLE_REGEXP.search(link_title)
            if not episode_title_match:
                log.warning("Error while parsing serial page: title `{0}` are not matched".format(link_title))
                continue

            season = int(episode_title_match.group(3))
            first_episode = int(episode_title_match.group(4))
            last_episode = first_episode
            last_episode_group = episode_title_match.group(5)
            if last_episode_group:
                last_episode = int(last_episode_group)

            ru_title = episode_title_match.group(1)
            title = episode_title_match.group(2)
            quality = episode_title_match.group(6)

            if last_episode > first_episode:
                episode_id = 's{0:02d}e{1:02d}-{2:02d}'.format(season, first_episode, last_episode)
            else:
                episode_id = 's{0:02d}e{1:02d}'.format(season, first_episode)

            entry_title = "{0} / {1} / {2} / {3}".format(title, ru_title, episode_id, quality)
            entry_url = link_node.get('href')

            entries.append({
                'season': season,
                'episode': first_episode,
                'ep': episode_id,
                'title': entry_title,
                'url': entry_url
            })

        return entries


class BaibakoDatabase(object):
    @staticmethod
    def shows_timestamp(db_session):
        shows_timestamp = db_session.query(func.min(DbBaibakoShow.updated_at)).scalar() or None
        return shows_timestamp

    @staticmethod
    def shows_count(db_session):
        return db_session.query(DbBaibakoShow).count()

    @staticmethod
    def clear_shows(db_session):
        db_session.query(DbBaibakoShowAlternateName).delete()
        db_session.query(DbBaibakoShow).delete()
        db_session.commit()

    @staticmethod
    def update_shows(shows, db_session):
        # Clear database
        BaibakoDatabase.clear_shows(db_session)

        # Insert new rows
        if shows and len(shows) > 0:
            now = datetime.now()
            for show in shows:
                db_show = DbBaibakoShow(id=show['show_id'], title=show['titles'][0], url=show['url'], updated_at=now)
                db_session.add(db_show)

                for index, item in enumerate(show['titles'][1:], start=1):
                    alternate_name = DbBaibakoShowAlternateName(show_id=show['show_id'], title=item)
                    db_session.add(alternate_name)

            db_session.commit()

    @staticmethod
    def get_shows(db_session):
        shows = list()

        db_shows = db_session.query(DbBaibakoShow).all()
        for db_show in db_shows:
            titles = list()
            titles.append(db_show.title)

            db_alternate_names = db_session.query(DbBaibakoShowAlternateName).filter(
                DbBaibakoShowAlternateName.show_id == db_show.id).all()
            if db_alternate_names and len(db_alternate_names) > 0:
                for db_alternate_name in db_alternate_names:
                    titles.append(db_alternate_name.title)

            shows.append({
                'show_id': db_show.id,
                'titles': titles,
                'url': db_show.url
            })

        return shows

    @staticmethod
    def get_show_by_id(show_id, db_session):
        db_show = db_session.query(DbBaibakoShow).filter(DbBaibakoShow.id == show_id).first()
        if db_show:
            titles = list()
            titles.append(db_show.title)

            db_alternate_names = db_session.query(DbBaibakoShowAlternateName).filter(
                DbBaibakoShowAlternateName.show_id == db_show.id).all()
            if db_alternate_names and len(db_alternate_names) > 0:
                for db_alternate_name in db_alternate_names:
                    titles.append(db_alternate_name.title)

            return {
                'show_id': db_show.id,
                'titles': titles,
                'url': db_show.url
            }

        return None

    @staticmethod
    def find_show_by_title(title, db_session):
        db_show = db_session.query(DbBaibakoShow).filter(DbBaibakoShow.title == title).first()
        if db_show:
            return BaibakoDatabase.get_show_by_id(db_show.id, db_session)

        db_alternate_name = db_session.query(DbBaibakoShowAlternateName).filter(
            DbBaibakoShowAlternateName.title == title).first()
        if db_alternate_name:
            return BaibakoDatabase.get_show_by_id(db_alternate_name.show_id, db_session)

        return None

    @staticmethod
    def find_episode(show_id, season, episode, db_session):
        return db_session.query(DbBaibakoEpisode).filter(
            DbBaibakoEpisode.show_id == show_id,
            DbBaibakoEpisode.season == season,
            DbBaibakoEpisode.episode == episode).first()

    @staticmethod
    def insert_episode(show_id, season, episode, title, url, db_session):
        db_episode = BaibakoDatabase.find_episode(show_id, season, episode, db_session)
        now = datetime.now()
        if not db_episode:
            db_episode = DbBaibakoEpisode(
                show_id=show_id,
                season=season,
                episode=episode)
        db_episode.title = title
        db_episode.url = url
        db_episode.timestamp = now

        db_session.add(db_episode)
        db_session.commit()

        return db_episode


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
        return DETAILS_URL_REGEXP.match(url)

    def url_rewrite(self, task, entry):
        url = entry['url']
        url_match = DETAILS_URL_REGEXP.search(url)
        if not url_match:
            reject_reason = "Url don't matched: {0}".format(url)
            log.verbose(reject_reason)
            # entry.reject(reject_reason)
            return False

        topic_id = url_match.group(1)
        url = 'http://baibako.tv/download.php?id={0}'.format(topic_id)
        entry['url'] = url
        return True

    def get_shows(self, task):
        serials_url = 'http://baibako.tv/serials.php'

        log.debug("Fetching serials page `{0}`...".format(serials_url))

        try:
            serials_response = task.requests.get(serials_url)
        except requests.RequestException as e:
            log.error("Error while fetching page: {0}".format(e))
            sleep(3)
            return None
        serials_html = serials_response.text
        sleep(3)

        log.debug("Parsing serials page `{0}`...".format(serials_url))

        shows = BaibakoParser.parse_shows_page(serials_html)
        if shows:
            for show in shows:
                show['url'] = process_url(show['url'], serials_response.url)

        return shows

    def search_show(self, task, title, db_session):
        update_required = True
        db_timestamp = BaibakoDatabase.shows_timestamp(db_session)
        if db_timestamp:
            difference = datetime.now() - db_timestamp
            update_required = difference.days > 3
        if update_required:
            log.debug('Update shows...')
            shows = self.get_shows(task)
            if shows:
                log.debug('{0} show(s) received'.format(len(shows)))
                BaibakoDatabase.update_shows(shows, db_session)

        show = BaibakoDatabase.find_show_by_title(title, db_session)
        return show

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

            show = self.search_show(task, search_title, db_session)
            if not show:
                continue

            db_episode = BaibakoDatabase.find_episode(show['show_id'], search_season, search_episode, db_session)
            if not db_episode:
                serial_url = show['url'] + '&tab=' + serial_tab
                try:
                    serial_response = task.requests.get(serial_url)
                except requests.RequestException as e:
                    log.error("Error while fetching page: {0}".format(e))
                    sleep(3)
                    continue
                serial_html = serial_response.text
                sleep(3)

                try:
                    parsed_episodes = BaibakoParser.parse_episodes_page(serial_html)
                except Exception as e:
                    log.error("Error while parsing episodes page: {0}".format(e))
                    continue

                for parsed_episode in parsed_episodes:
                    season = parsed_episode['season']
                    episode = parsed_episode['episode']

                    url = parsed_episode['url']
                    url = process_url(url, serial_response.url)

                    db_updated_episode = BaibakoDatabase.insert_episode(
                        show['show_id'], season, episode, parsed_episode['title'], url, db_session)

                    if season == search_season and episode == search_episode:
                        # (first_episode > search_episode or last_episode < search_episode):
                        db_episode = db_updated_episode

            if db_episode:
                entry = Entry()
                entry['title'] = db_episode.title
                entry['url'] = db_episode.url
                entries.add(entry)

        return entries


# endregion


def reset_cache(manager):
    db_session = Session()
    db_session.query(DbBaibakoEpisode).delete()
    db_session.query(DbBaibakoShowAlternateName).delete()
    db_session.query(DbBaibakoShow).delete()
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
