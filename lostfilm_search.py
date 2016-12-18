# -*- coding: utf-8 -*-
from __future__ import unicode_literals, division, absolute_import
from builtins import *  # pylint: disable=unused-import, redefined-builtin

import os
import importlib.util
import logging
import re
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


PLUGIN_NAME = 'lostfilm_search'

dir_path = os.path.dirname(os.path.abspath(__file__))

module_name = 'lostfilm_utils'
module_path = os.path.join(dir_path, module_name + '.py')
spec = importlib.util.spec_from_file_location(module_name, module_path)
lostfilm_utils = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lostfilm_utils)


Base = versioned_base(PLUGIN_NAME, 0)
log = logging.getLogger(PLUGIN_NAME)


class DbLostFilmShow(Base):
    __tablename__ = 'lostfilm_shows'
    id = Column(Integer, primary_key=True, nullable=False)
    title = Column(Unicode, index=True, nullable=False)
    url = Column(Unicode, nullable=False)
    updated_at = Column(DateTime, nullable=False)


class DbLostFilmShowAlternateName(Base):
    __tablename__ = 'lostfilm_show_alternate_names'
    id = Column(Integer, primary_key=True, autoincrement=True, nullable=False)
    show_id = Column(Integer, ForeignKey('lostfilm_shows.id'), nullable=False)
    title = Column(Unicode, index=True, nullable=False)
    __table_args__ = (UniqueConstraint('show_id', 'title', name='_show_title_uc'),)


class LostFilmShow(object):
    def __init__(self, show_id, titles, url):
        self.show_id = show_id
        self.titles = titles
        self.url = url


class LostFilmParser(object):
    @staticmethod
    def parse_shows_page(html):
        serials_tree = BeautifulSoup(html, 'html.parser')
        mid_node = serials_tree.find('div', class_='mid')
        if not mid_node:
            log.error("Error while parsing details page: node <div class=`mid`> are not found")
            return None

        shows = set()

        url_regexp = re.compile(r'cat=(\d+)', flags=re.IGNORECASE)
        link_nodes = mid_node.find_all('a', class_='bb_a')
        for link_node in link_nodes:
            category_link = link_node.get('href')
            category_link = lostfilm_utils.add_host_if_need(category_link)

            url_match = url_regexp.search(category_link)
            if not url_match:
                continue

            show_id = int(url_match.group(1))

            link_text = link_node.get_text(separator='\n')
            titles = link_text.splitlines()
            if len(titles) <= 0:
                log.error("No titles are found")
                continue

            titles = [x.strip('()') for x in titles]

            # log.debug("Serial `{0}` was added".format(" / ".join(titles)))
            show = LostFilmShow(show_id=show_id, titles=titles, url=category_link)
            shows.add(show)

        log.debug("{0:d} show(s) are found".format(len(shows)))
        return shows


class LostFilmDatabase(object):
    @staticmethod
    def shows_timestamp(db_session):
        shows_timestamp = db_session.query(func.min(DbLostFilmShow.updated_at)).scalar() or None
        return shows_timestamp

    @staticmethod
    def shows_count(db_session):
        return db_session.query(DbLostFilmShow).count()

    @staticmethod
    def clear_shows(db_session):
        db_session.query(DbLostFilmShowAlternateName).delete()
        db_session.query(DbLostFilmShow).delete()
        db_session.commit()

    @staticmethod
    def update_shows(shows, db_session):
        # Clear database
        LostFilmDatabase.clear_shows(db_session)

        # Insert new rows
        if shows and len(shows) > 0:
            now = datetime.now()
            for show in shows:
                db_show = DbLostFilmShow(id=show.show_id, title=show.titles[0], url=show.url, updated_at=now)
                db_session.add(db_show)

                for index, item in enumerate(show.titles[1:], start=1):
                    alternate_name = DbLostFilmShowAlternateName(show_id=show.show_id, title=item)
                    db_session.add(alternate_name)

            db_session.commit()

    @staticmethod
    def get_shows(db_session):
        shows = set()

        db_shows = db_session.query(DbLostFilmShow).all()
        for db_show in db_shows:
            titles = list()
            titles.append(db_show.title)

            db_alternate_names = db_session.query(DbLostFilmShowAlternateName).filter(
                DbLostFilmShowAlternateName.show_id == db_show.id).all()
            if db_alternate_names and len(db_alternate_names) > 0:
                for db_alternate_name in db_alternate_names:
                    titles.append(db_alternate_name.title)

            show = LostFilmShow(show_id=db_show.id, titles=titles, url=db_show.url)
            shows.add(show)

        return shows

    @staticmethod
    def get_show_by_id(show_id, db_session):
        db_show = db_session.query(DbLostFilmShow).filter(DbLostFilmShow.id == show_id).first()
        if db_show:
            titles = list()
            titles.append(db_show.title)

            db_alternate_names = db_session.query(DbLostFilmShowAlternateName).filter(
                DbLostFilmShowAlternateName.show_id == db_show.id).all()
            if db_alternate_names and len(db_alternate_names) > 0:
                for db_alternate_name in db_alternate_names:
                    titles.append(db_alternate_name.title)

            show = LostFilmShow(show_id=db_show.id, titles=titles, url=db_show.url)
            return show

        return None

    @staticmethod
    def find_show_by_title(title, db_session):
        db_show = db_session.query(DbLostFilmShow).filter(DbLostFilmShow.title == title).first()
        if db_show:
            return LostFilmDatabase.get_show_by_id(db_show.id, db_session)

        db_alternate_name = db_session.query(DbLostFilmShowAlternateName).filter(
            DbLostFilmShowAlternateName.title == title).first()
        if db_alternate_name:
            return LostFilmDatabase.get_show_by_id(db_alternate_name.show_id, db_session)

        return None


class LostFilmSearch(object):

    def get_shows(self, task):
        serials_url = 'http://www.lostfilm.tv/serials.php'

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

        shows = LostFilmParser.parse_shows_page(serials_html)
        return shows

    def search_show(self, task, title, db_session):
        update_required = True
        db_timestamp = LostFilmDatabase.shows_timestamp(db_session)
        if db_timestamp:
            difference = datetime.now() - db_timestamp
            update_required = difference.days > 3
        if update_required:
            log.debug('Update shows...')
            shows = self.get_shows(task)
            if shows:
                log.debug('{0} show(s) received'.format(len(shows)))
                LostFilmDatabase.update_shows(shows, db_session)

        show = LostFilmDatabase.find_show_by_title(title, db_session)
        return show

    def search(self, task, entry, config=None):
        entries = set()

        db_session = Session()

        ep_regexp = re.compile(r"(\d+)\s+[Сс]езон\s+(\d+)\s+[Сс]ерия", flags=re.IGNORECASE)
        row_regexp = re.compile(r't_row.*', flags=re.IGNORECASE)
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

            try:
                category_response = task.requests.get(show.url)
            except requests.RequestException as e:
                log.error("Error while fetching page: {0}".format(e))
                sleep(3)
                continue
            category_html = category_response.content
            sleep(3)

            category_tree = BeautifulSoup(category_html, 'html.parser')
            mid_node = category_tree.find('div', class_='mid')

            row_nodes = mid_node.find_all('div', class_=row_regexp)
            for row_node in row_nodes:
                ep_node = row_node.find('span', class_='micro')
                if not ep_node:
                    continue

                ep_match = ep_regexp.search(ep_node.get_text())
                if not ep_match:
                    continue

                season = int(ep_match.group(1))
                episode = int(ep_match.group(2))
                if season != search_season or episode != search_episode:
                    continue

                details_node = row_node.find('a', class_='a_details')
                if not details_node:
                    continue

                details_url = details_node.get('href')
                details_url = lostfilm_utils.add_host_if_need(details_url)

                entry = Entry()
                entry['title'] = "{0} / s{1:02d}e{2:02d}".format(search_title, season, episode)
                # entry['series_season'] = season
                # entry['series_episode'] = episode
                entry['url'] = details_url
                # tds = link.parent.parent.parent.find_all('td')
                # entry['torrent_seeds'] = int(tds[-2].contents[0])
                # entry['torrent_leeches'] = int(tds[-1].contents[0])
                # entry['search_sort'] = torrent_availability(entry['torrent_seeds'], entry['torrent_leeches'])
                # Parse content_size
                # size = link.find_next(attrs={'class': 'detDesc'}).get_text()
                # size = re.search('Size (\d+(\.\d+)?\xa0(?:[PTGMK])iB)', size)
                #
                # entry['content_size'] = parse_filesize(size.group(1))

                entries.add(entry)

        return entries


@event('plugin.register')
def register_plugin():
    plugin.register(LostFilmSearch, PLUGIN_NAME, groups=['search'], api_ver=2)
