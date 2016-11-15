# -*- coding: utf-8 -*-
from __future__ import unicode_literals, division, absolute_import
from builtins import *  # pylint: disable=unused-import, redefined-builtin

import re
import logging
from time import sleep
from bs4 import BeautifulSoup

from flexget import plugin
from flexget.entry import Entry
from flexget.event import event
from flexget.utils import requests


plugin_name = 'alexfilm_search'
log = logging.getLogger(plugin_name)


class AlexFilmShow(object):
    titles = []
    url = ''

    def __init__(self, titles, url):
        self.titles = titles
        self.url = url


class AlexFilmSearch(object):

    def search(self, task, entry, config=None):
        entries = set()

        serials_url = 'http://alexfilm.cc/'

        try:
            serials_response = task.requests.get(serials_url)
        except requests.RequestException as e:
            log.error("Error while fetching page: {0}".format(e))
            sleep(3)
            return None
        serials_html = serials_response.text
        sleep(3)

        shows = set()

        serials_tree = BeautifulSoup(serials_html, 'html.parser')
        serials_node = serials_tree.find('div', id='sidebar')
        if not serials_node:
            log.error('Error while parsing serials page: node <table class=`table.*`> are not found')
            return None

        serials_node = serials_node.find('div')
        if not serials_node:
            log.error('Error while parsing serials page: node <table class=`table.*`> are not found')
            return None

        url_regexp = re.compile(r'f=(\d+)', flags=re.IGNORECASE)
        url_nodes = serials_node.find_all('a', href=url_regexp)
        for url_node in url_nodes:
            title = url_node.text
            titles = title.split(' / ')
            url = 'http://alexfilm.cc/' + url_node.get('href')

            show = AlexFilmShow(titles, url)
            shows.add(show)

            log.debug("{0} - {1}".format(show.titles, show.url))

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

            for show in shows:
                if search_title not in show.titles:
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

                panel_nodes = serial_table_node.find_all('div', class_='panel panel-default')
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

                    name = "{0} / {1} / s{2:02d}e{3:02d}-e{4:02d} / {5}".format(
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
