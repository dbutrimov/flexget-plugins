# -*- coding: utf-8 -*-
from __future__ import unicode_literals, division, absolute_import
from builtins import *  # pylint: disable=unused-import, redefined-builtin

import os
import importlib.util
import re
import logging
from time import sleep
from bs4 import BeautifulSoup
from datetime import datetime

from sqlalchemy import Column, Unicode, Integer, DateTime, UniqueConstraint, ForeignKey, func

from flexget import plugin
from flexget.entry import Entry
from flexget.event import event
from flexget.utils import requests
from flexget.manager import Session
from flexget.db_schema import versioned_base


plugin_name = 'baibako_search'

dir_path = os.path.dirname(os.path.abspath(__file__))

module_name = 'baibako_utils'
module_path = os.path.join(dir_path, module_name + '.py')
spec = importlib.util.spec_from_file_location(module_name, module_path)
baibako_utils = importlib.util.module_from_spec(spec)
spec.loader.exec_module(baibako_utils)


Base = versioned_base(plugin_name, 0)
log = logging.getLogger(plugin_name)

table_class_regexp = re.compile(r'table.*', flags=re.IGNORECASE)
episode_title_regexp = re.compile(
    r'^([^/]*?)\s*/\s*([^/]*?)\s*/\s*s(\d+)e(\d+)(?:-(\d+))?\s*/\s*([^/]*?)\s*(?:(?:/.*)|$)',
    flags=re.IGNORECASE)


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


class BaibakoShow(object):
    def __init__(self, show_id, titles, url):
        self.show_id = show_id
        self.titles = titles
        self.url = url


class BaibakoParser(object):
    @staticmethod
    def parse_shows_page(html):
        serials_tree = BeautifulSoup(html, 'html.parser')
        serials_node = serials_tree.find('table', class_=table_class_regexp)
        if not serials_node:
            log.error('Error while parsing serials page: node <table class=`table.*`> are not found')
            return None

        shows = set()

        url_regexp = re.compile(r'id=(\d+)', flags=re.IGNORECASE)
        link_nodes = serials_node.find_all('a')
        for link_node in link_nodes:
            serial_link = link_node.get('href')
            serial_link = baibako_utils.add_host_if_need(serial_link)

            url_match = url_regexp.search(serial_link)
            if not url_match:
                continue

            show_id = int(url_match.group(1))

            serial_title = link_node.text

            show = BaibakoShow(show_id=show_id, titles=[serial_title], url=serial_link)
            shows.add(show)

        log.debug("{0:d} show(s) are found".format(len(shows)))
        return shows


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
                db_show = DbBaibakoShow(id=show.show_id, title=show.titles[0], url=show.url, updated_at=now)
                db_session.add(db_show)

                for index, item in enumerate(show.titles[1:], start=1):
                    alternate_name = DbBaibakoShowAlternateName(show_id=show.show_id, title=item)
                    db_session.add(alternate_name)

            db_session.commit()

    @staticmethod
    def get_shows(db_session):
        shows = set()

        db_shows = db_session.query(DbBaibakoShow).all()
        for db_show in db_shows:
            titles = list()
            titles.append(db_show.title)

            db_alternate_names = db_session.query(DbBaibakoShowAlternateName).filter(
                DbBaibakoShowAlternateName.show_id == db_show.id).all()
            if db_alternate_names and len(db_alternate_names) > 0:
                for db_alternate_name in db_alternate_names:
                    titles.append(db_alternate_name.title)

            show = BaibakoShow(show_id=db_show.id, titles=titles, url=db_show.url)
            shows.add(show)

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

            show = BaibakoShow(show_id=db_show.id, titles=titles, url=db_show.url)
            return show

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


class BaibakoSearch(object):
    """Usage:

    baibako_search:
      serial_tab: 'hd720' or 'hd1080' or 'x264' or 'xvid' or 'all'
    """

    schema = {
        'type': 'object',
        'properties': {
            'serial_tab': {'type': 'string'}
        },
        'additionalProperties': False
    }

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
        episode_link_regexp = re.compile(r'details.php\?id=(\d+)', flags=re.IGNORECASE)

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

            serial_url = show.url + '&tab=' + serial_tab
            try:
                serial_response = task.requests.get(serial_url)
            except requests.RequestException as e:
                log.error("Error while fetching page: {0}".format(e))
                sleep(3)
                continue
            serial_html = serial_response.text
            sleep(3)

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
                    episode_id = 's{0:02d}e{1:02d}-{2:02d}'.format(season, first_episode, last_episode)
                else:
                    episode_id = 's{0:02d}e{1:02d}'.format(season, first_episode)

                entry_title = "{0} / {1} / {2} / {3}".format(title, ru_title, episode_id, quality)
                entry_url = link_node.get('href')
                entry_url = baibako_utils.add_host_if_need(entry_url)

                entry = Entry()
                entry['title'] = entry_title
                entry['url'] = entry_url

                entries.add(entry)

        return entries


@event('plugin.register')
def register_plugin():
    plugin.register(BaibakoSearch, plugin_name, groups=['search'], api_ver=2)
