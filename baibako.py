# -*- coding: utf-8 -*-
from __future__ import unicode_literals, division, absolute_import
from builtins import *  # pylint: disable=unused-import, redefined-builtin

import re
from bs4 import BeautifulSoup
import urllib

import logging

from flexget import plugin
from flexget.entry import Entry
from flexget.event import event
from flexget.utils import requests

log = logging.getLogger('baibako')

host_prefix = 'http://baibako.tv/'
host_regexp = re.compile(r'^https?://(?:www\.)?baibako\.tv.*$', flags=re.IGNORECASE)

details_url_regexp = re.compile(r'^https?://(?:www\.)?baibako\.tv/details\.php\?id=(\d+).*$', flags=re.IGNORECASE)

table_class_regexp = re.compile(r'table.*', flags=re.IGNORECASE)

episode_title_regexp = re.compile(
    r'^([^/]*?)\s*/\s*([^/]*?)\s*/\s*s(\d+)e(\d+)(?:-(\d+))?\s*/\s*([^/]*?)\s*(?:(?:/.*)|$)',
    flags=re.IGNORECASE)


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
            serial_tab: 'hd720'
        """

    config = {}

    schema = {
        'type': 'object',
        'properties': {
            'serial_tab': {'type': 'string'}
        },
        'additionalProperties': False
    }

    def add_host_if_need(self, url):
        if not host_regexp.match(url):
            url = urllib.parse.urljoin(host_prefix, url)
        return url

    def on_task_start(self, task, config):
        if not isinstance(config, dict):
            log.verbose("Config was not determined - use default.")
        else:
            self.config = config

    def url_rewritable(self, task, entry):
        url = entry['url']
        return details_url_regexp.match(url)

    def url_rewrite(self, task, entry):
        url = entry['url']
        url_match = details_url_regexp.search(url)
        if not url_match:
            reject_reason = 'Url don''t matched'
            log.error(reject_reason)
            entry.reject(reject_reason)
            return False

        topic_id = url_match.group(1)
        url = 'http://baibako.tv/download.php?id={0}'.format(topic_id)
        entry['url'] = url
        return True

    def search(self, task, entry, config=None):
        entries = set()

        serials_url = 'http://baibako.tv/serials.php'

        log.debug("Fetching serials page `{0}`...".format(serials_url))

        try:
            serials_response = task.requests.get(serials_url)
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

        serial_tab = self.config.get('serial_tab')
        if not isinstance(serial_tab, str):
            serial_tab = 'all'

        search_regexp = re.compile(r'^(.*?)\s*s(\d+?)e(\d+?)$', flags=re.IGNORECASE)
        episode_link_regexp = re.compile(r'details.php\?id=(\d+)', flags=re.IGNORECASE)

        for search_string in entry.get('search_strings', [entry['title']]):
            search_match = search_regexp.search(search_string)
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
                    serial_response = task.requests.get(serial_url)
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
                        episode_number = 's{0:02d}e{1:02d}-{2:02d}'.format(season, first_episode, last_episode)
                    else:
                        episode_number = 's{0:02d}e{1:02d}'.format(season, first_episode)

                    entry_title = "{0} / {1} / {2} / {3}".format(title, ru_title, episode_number, quality)
                    entry_url = link_node.get('href')
                    entry_url = self.add_host_if_need(entry_url)

                    entry = Entry()
                    entry['title'] = entry_title
                    entry['url'] = entry_url
                    # entry['series_name'] = [title, ru_title]
                    # entry['series_season'] = season
                    # if last_episode > first_episode:
                    #     entry['series_episode'] = '{0}-{1}'.format(first_episode, last_episode)
                    # else:
                    #     entry['series_episode'] = first_episode
                    # entry['series_id'] = episode_number
                    # entry['proper'] = 'repack'

                    entries.add(entry)

        return entries


@event('plugin.register')
def register_plugin():
    plugin.register(BaibakoUrlRewrite, 'baibako', groups=['urlrewriter', 'search'], api_ver=2)
