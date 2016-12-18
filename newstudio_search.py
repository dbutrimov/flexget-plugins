# -*- coding: utf-8 -*-
from __future__ import unicode_literals, division, absolute_import
from builtins import *  # pylint: disable=unused-import, redefined-builtin

import os
import importlib.util
import re
from time import sleep
from bs4 import BeautifulSoup
import logging
from datetime import datetime

from sqlalchemy import Column, Unicode, Integer, DateTime, UniqueConstraint, ForeignKey, func

from flexget import plugin
from flexget.entry import Entry
from flexget.event import event
from flexget.utils import requests
from flexget.manager import Session
from flexget.db_schema import versioned_base


plugin_name = 'newstudio_search'

dir_path = os.path.dirname(os.path.abspath(__file__))

module_name = 'newstudio_utils'
module_path = os.path.join(dir_path, module_name + '.py')
spec = importlib.util.spec_from_file_location(module_name, module_path)
newstudio_utils = importlib.util.module_from_spec(spec)
spec.loader.exec_module(newstudio_utils)


Base = versioned_base(plugin_name, 0)
log = logging.getLogger(plugin_name)

ep_regexp = re.compile(r"\([Сс]езон\s+(\d+)\W+[Cс]ерия\s+(\d+)\)", flags=re.IGNORECASE)


class DbNewStudioShow(Base):
    __tablename__ = 'newstudio_shows'
    id = Column(Integer, primary_key=True, nullable=False)
    title = Column(Unicode, index=True, nullable=False)
    url = Column(Unicode, nullable=False)
    updated_at = Column(DateTime, nullable=False)


class DbNewStudioShowAlternateName(Base):
    __tablename__ = 'newstudio_show_alternate_names'
    id = Column(Integer, primary_key=True, autoincrement=True, nullable=False)
    show_id = Column(Integer, ForeignKey('newstudio_shows.id'), nullable=False)
    title = Column(Unicode, index=True, nullable=False)
    __table_args__ = (UniqueConstraint('show_id', 'title', name='_show_title_uc'),)


class NewStudioShow(object):
    def __init__(self, show_id, titles, url):
        self.show_id = show_id
        self.titles = titles
        self.url = url


class NewStudioParser(object):
    @staticmethod
    def parse_shows_page(html):
        serials_tree = BeautifulSoup(html, 'html.parser')
        accordion_node = serials_tree.find('div', class_='accordion', id='serialist')
        if not accordion_node:
            log.error("Error while parsing serials page: node <div class=`accordion` id=`serialist`> are not found")
            return None

        shows = set()

        url_regexp = re.compile(r'f=(\d+)', flags=re.IGNORECASE)
        inner_nodes = accordion_node.find_all('div', class_='accordion-inner')
        for inner_node in inner_nodes:
            link_nodes = inner_node.find_all('a')
            for link_node in link_nodes:
                viewforum_link = link_node.get('href')
                viewforum_link = newstudio_utils.add_host_if_need(viewforum_link)

                url_match = url_regexp.search(viewforum_link)
                if not url_match:
                    continue

                show_id = int(url_match.group(1))

                title = link_node.text

                show = NewStudioShow(show_id=show_id, titles=[title], url=viewforum_link)
                shows.add(show)

        log.debug("{0:d} shows are found".format(len(shows)))
        return shows


class NewStudioDatabase(object):
    @staticmethod
    def shows_timestamp(db_session):
        shows_timestamp = db_session.query(func.min(DbNewStudioShow.updated_at)).scalar() or None
        return shows_timestamp

    @staticmethod
    def shows_count(db_session):
        return db_session.query(DbNewStudioShow).count()

    @staticmethod
    def clear_shows(db_session):
        db_session.query(DbNewStudioShowAlternateName).delete()
        db_session.query(DbNewStudioShow).delete()
        db_session.commit()

    @staticmethod
    def update_shows(shows, db_session):
        # Clear database
        NewStudioDatabase.clear_shows(db_session)

        # Insert new rows
        if shows and len(shows) > 0:
            now = datetime.now()
            for show in shows:
                db_show = DbNewStudioShow(id=show.show_id, title=show.titles[0], url=show.url, updated_at=now)
                db_session.add(db_show)

                for index, item in enumerate(show.titles[1:], start=1):
                    alternate_name = DbNewStudioShowAlternateName(show_id=show.show_id, title=item)
                    db_session.add(alternate_name)

            db_session.commit()

    @staticmethod
    def get_shows(db_session):
        shows = set()

        db_shows = db_session.query(DbNewStudioShow).all()
        for db_show in db_shows:
            titles = list()
            titles.append(db_show.title)

            db_alternate_names = db_session.query(DbNewStudioShowAlternateName).filter(
                DbNewStudioShowAlternateName.show_id == db_show.id).all()
            if db_alternate_names and len(db_alternate_names) > 0:
                for db_alternate_name in db_alternate_names:
                    titles.append(db_alternate_name.title)

            show = NewStudioShow(show_id=db_show.id, titles=titles, url=db_show.url)
            shows.add(show)

        return shows

    @staticmethod
    def get_show_by_id(show_id, db_session):
        db_show = db_session.query(DbNewStudioShow).filter(DbNewStudioShow.id == show_id).first()
        if db_show:
            titles = list()
            titles.append(db_show.title)

            db_alternate_names = db_session.query(DbNewStudioShowAlternateName).filter(
                DbNewStudioShowAlternateName.show_id == db_show.id).all()
            if db_alternate_names and len(db_alternate_names) > 0:
                for db_alternate_name in db_alternate_names:
                    titles.append(db_alternate_name.title)

            show = NewStudioShow(show_id=db_show.id, titles=titles, url=db_show.url)
            return show

        return None

    @staticmethod
    def find_show_by_title(title, db_session):
        db_show = db_session.query(DbNewStudioShow).filter(DbNewStudioShow.title == title).first()
        if db_show:
            return NewStudioDatabase.get_show_by_id(db_show.id, db_session)

        db_alternate_name = db_session.query(DbNewStudioShowAlternateName).filter(
            DbNewStudioShowAlternateName.title == title).first()
        if db_alternate_name:
            return NewStudioDatabase.get_show_by_id(db_alternate_name.show_id, db_session)

        return None


class NewStudioSearch(object):

    def get_shows(self, task):
        serials_url = 'http://newstudio.tv/'

        log.debug("Fetching serials page `{0}`...".format(serials_url))

        try:
            serials_response = task.requests.get(serials_url)
        except requests.RequestException as e:
            log.error("Error while fetching page: {0}".format(e))
            sleep(3)
            return None
        serials_html = serials_response.content
        sleep(3)

        log.debug("Parsing serials page `{0}`...".format(serials_url))

        shows = NewStudioParser.parse_shows_page(serials_html)
        return shows

    def search_show(self, task, title, db_session):
        update_required = True
        db_timestamp = NewStudioDatabase.shows_timestamp(db_session)
        if db_timestamp:
            difference = datetime.now() - db_timestamp
            update_required = difference.days > 3
        if update_required:
            log.debug('Update shows...')
            shows = self.get_shows(task)
            if shows:
                log.debug('{0} show(s) received'.format(len(shows)))
                NewStudioDatabase.update_shows(shows, db_session)

        show = NewStudioDatabase.find_show_by_title(title, db_session)
        return show

    def search(self, task, entry, config=None):
        entries = set()

        db_session = Session()

        viewtopic_link_regexp = re.compile(r'.*/viewtopic\.php\?t=(\d+).*', flags=re.IGNORECASE)
        pagination_regexp = re.compile(r'pagination.*', flags=re.IGNORECASE)
        quality_regexp = re.compile(r'^.*\)\s*(.*?)$', flags=re.IGNORECASE)
        search_regexp = re.compile(r'^(.*?)\s*s(\d+?)e(\d+?)$', flags=re.IGNORECASE)
        for search_string in entry.get('search_strings', [entry['title']]):
            search_match = search_regexp.search(search_string)
            if not search_match:
                continue

            search_title = search_match.group(1)
            search_season = int(search_match.group(2))
            search_episode = int(search_match.group(3))

            log.debug("{0} s{1:02d}e{2:02d}".format(search_title, search_season, search_episode))

            show = self.search_show(task, search_title, db_session)
            if not show:
                continue

            show_pages = [show.url]
            page_index = 0
            while page_index < len(show_pages):
                current_page_index = page_index
                page_index += 1

                page_url = show_pages[current_page_index]
                try:
                    viewforum_response = task.requests.get(page_url)
                except requests.RequestException as e:
                    log.error("Error while fetching page: {0}".format(e))
                    sleep(3)
                    continue
                viewforum_html = viewforum_response.content
                sleep(3)

                viewforum_tree = BeautifulSoup(viewforum_html, 'html.parser')

                if current_page_index < 1:
                    pagination_node = viewforum_tree.find('div', class_=pagination_regexp)
                    if pagination_node:
                        pagination_link_nodes = pagination_node.find_all('a')
                        for pagination_link_node in pagination_link_nodes:
                            page_number_text = pagination_link_node.text
                            try:
                                int(page_number_text)
                            except Exception:
                                continue
                            page_link = pagination_link_node.get('href')
                            page_link = newstudio_utils.add_host_if_need(page_link)
                            show_pages.append(page_link)

                accordion_node = viewforum_tree.find('div', class_='accordion-inner')
                if not accordion_node:
                    continue

                row_nodes = accordion_node.find_all('div', class_='row-fluid')
                for row_node in row_nodes:
                    link_node = row_node.find('a', class_='torTopic tt-text', href=viewtopic_link_regexp)
                    if not link_node:
                        continue

                    title = link_node.text
                    ep_match = ep_regexp.search(title)
                    if not ep_match:
                        continue

                    season = int(ep_match.group(1))
                    episode = int(ep_match.group(2))
                    # log.debug("{0} (s{1:02d}e{2:02d})".format(title, season, episode))
                    if season != search_season or episode != search_episode:
                        continue

                    quality = None
                    quality_match = quality_regexp.search(title)
                    if quality_match:
                        quality = quality_match.group(1)

                    torrent_url = link_node.get('href')
                    torrent_url = newstudio_utils.add_host_if_need(torrent_url)

                    entry = Entry()
                    entry['title'] = "{0} / s{1:02d}e{2:02d} / {3}".format(search_title, season, episode, quality)
                    entry['url'] = torrent_url

                    entries.add(entry)

        return entries


@event('plugin.register')
def register_plugin():
    plugin.register(NewStudioSearch, plugin_name, groups=['search'], api_ver=2)
