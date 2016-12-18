# -*- coding: utf-8 -*-
from __future__ import unicode_literals, division, absolute_import
from builtins import *  # pylint: disable=unused-import, redefined-builtin

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


plugin_name = 'alexfilm_search'

Base = versioned_base(plugin_name, 0)
log = logging.getLogger(plugin_name)


class DbAlexFilmShow(Base):
    __tablename__ = 'alexfilm_shows'
    id = Column(Integer, primary_key=True, nullable=False)
    title = Column(Unicode, index=True, nullable=False)
    url = Column(Unicode, nullable=False)
    updated_at = Column(DateTime, nullable=False)


class DbAlexFilmShowAlternateName(Base):
    __tablename__ = 'alexfilm_show_alternate_names'
    id = Column(Integer, primary_key=True, autoincrement=True, nullable=False)
    show_id = Column(Integer, ForeignKey('alexfilm_shows.id'), nullable=False)
    title = Column(Unicode, index=True, nullable=False)
    __table_args__ = (UniqueConstraint('show_id', 'title', name='_show_title_uc'),)


class AlexFilmShow(object):
    def __init__(self, show_id, titles, url):
        self.show_id = show_id
        self.titles = titles
        self.url = url


class AlexFilmParser(object):
    @staticmethod
    def parse_shows_page(html):
        serials_tree = BeautifulSoup(html, 'html.parser')
        serials_node = serials_tree.find('ul', id='serials')
        if not serials_node:
            log.error('Error while parsing serials page: node <ul id=`serials`> are not found')
            return None

        shows = set()

        url_regexp = re.compile(r'f=(\d+)', flags=re.IGNORECASE)
        url_nodes = serials_node.find_all('a', href=url_regexp)
        for url_node in url_nodes:
            href = 'http://alexfilm.cc/' + url_node.get('href')
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
    def shows_timestamp(db_session):
        shows_timestamp = db_session.query(func.min(DbAlexFilmShow.updated_at)).scalar() or None
        return shows_timestamp

    @staticmethod
    def shows_count(db_session):
        return db_session.query(DbAlexFilmShow).count()

    @staticmethod
    def clear_shows(db_session):
        db_session.query(DbAlexFilmShowAlternateName).delete()
        db_session.query(DbAlexFilmShow).delete()
        db_session.commit()

    @staticmethod
    def update_shows(shows, db_session):
        # Clear database
        AlexFilmDatabase.clear_shows(db_session)

        # Insert new rows
        if shows and len(shows) > 0:
            now = datetime.now()
            for show in shows:
                db_show = DbAlexFilmShow(id=show.show_id, title=show.titles[0], url=show.url, updated_at=now)
                db_session.add(db_show)

                for index, item in enumerate(show.titles[1:], start=1):
                    alternate_name = DbAlexFilmShowAlternateName(show_id=show.show_id, title=item)
                    db_session.add(alternate_name)

            db_session.commit()

    @staticmethod
    def get_shows(db_session):
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
    def get_show_by_id(show_id, db_session):
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
    def find_show_by_title(title, db_session):
        db_show = db_session.query(DbAlexFilmShow).filter(DbAlexFilmShow.title == title).first()
        if db_show:
            return AlexFilmDatabase.get_show_by_id(db_show.id, db_session)

        db_alternate_name = db_session.query(DbAlexFilmShowAlternateName).filter(
            DbAlexFilmShowAlternateName.title == title).first()
        if db_alternate_name:
            return AlexFilmDatabase.get_show_by_id(db_alternate_name.show_id, db_session)

        return None


class AlexFilmSearch(object):

    def get_shows(self, task):
        serials_url = 'http://alexfilm.cc/'

        try:
            serials_response = task.requests.get(serials_url)
        except requests.RequestException as e:
            log.error("Error while fetching page: {0}".format(e))
            sleep(3)
            return None
        serials_html = serials_response.text
        sleep(3)

        shows = AlexFilmParser.parse_shows_page(serials_html)
        return shows

    def search_show(self, task, title, db_session):
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

    def search(self, task, entry, config=None):

        entries = set()

        db_session = Session()

        search_string_regexp = re.compile(r'^(.*?)\s*s(\d+)e(\d+)$', flags=re.IGNORECASE)
        topic_name_regexp = re.compile(
            r"^([^/]*?)\s*/\s*([^/]*?)\s/\s*[Сс]езон\s*(\d+)\s*/\s*[Сс]ерии\s*(\d+)-(\d+).*,\s*(.*)\s*\].*$",
            re.IGNORECASE)

        # regexp: '^([^/]*?)\s*/\s*([^/]*?)\s/\s*[Сс]езон\s*(\d+)\s*/\s*[Сс]ерии\s*(\d+)-(\d+).*,\s*(.*)\s*\].*$'
        # format: '\2 / \1 / s\3e\4-e\5 / \6'

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

            try:
                serial_response = task.requests.get(show.url)
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

            url_regexp = re.compile(r'viewtopic\.php\?t=(\d+)', flags=re.IGNORECASE)

            panel_class_regexp = re.compile(r'panel.*', flags=re.IGNORECASE)
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

                name = "{0} / {1} / s{2:02d}e{3:02d}-{4:02d} / {5}".format(
                    title, alternative_title, season, first_episode, last_episode, quality)
                topic_url = 'http://alexfilm.cc/' + url_node.get('href')

                log.debug("{0} - {1}".format(name, topic_url))

                entry = Entry()
                entry['title'] = name
                entry['url'] = topic_url

                entries.add(entry)

        return entries


@event('plugin.register')
def register_plugin():
    plugin.register(AlexFilmSearch, plugin_name, groups=['search'], api_ver=2)
