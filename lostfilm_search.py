# -*- coding: utf-8 -*-
from __future__ import unicode_literals, division, absolute_import
from builtins import *  # pylint: disable=unused-import, redefined-builtin

import os
import importlib.util
import logging
import re
from bs4 import BeautifulSoup

from flexget import plugin
from flexget.entry import Entry
from flexget.event import event
from flexget.utils import requests


dir_path = os.path.dirname(os.path.abspath(__file__))

module_path = os.path.join(dir_path, 'lostfilm_utils.py')
spec = importlib.util.spec_from_file_location('lostfilm_utils', module_path)
lostfilm_utils = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lostfilm_utils)


log = logging.getLogger('lostfilm_search')


class LostFilmShow(object):
    titles = []
    url = ''

    def __init__(self, titles, url):
        self.titles = titles
        self.url = url


class LostFilmSearch(object):

    def search(self, task, entry, config=None):
        entries = set()

        serials_url = 'http://www.lostfilm.tv/serials.php'

        log.debug("Fetching serials page `{0}`...".format(serials_url))

        try:
            serials_response = task.requests.get(serials_url)
        except requests.RequestException as e:
            log.error("Error while fetching page: {0}".format(e))
            return None
        serials_html = serials_response.content

        log.debug("Parsing serials page `{0}`...".format(serials_url))

        serials_tree = BeautifulSoup(serials_html, 'html.parser')
        mid_node = serials_tree.find('div', class_='mid')
        if not mid_node:
            log.error("Error while parsing details page: node <div class=`mid`> are not found")
            return None

        shows = set()
        link_nodes = mid_node.find_all('a', class_='bb_a')
        for link_node in link_nodes:
            link_text = link_node.get_text(separator='\n')
            titles = link_text.splitlines()
            if len(titles) <= 0:
                log.error("No titles are found")
                continue

            titles = [x.strip('()') for x in titles]
            category_link = link_node.get('href')
            category_link = lostfilm_utils.add_host_if_need(category_link)

            # log.debug("Serial `{0}` was added".format(" / ".join(titles)))
            show = LostFilmShow(titles, category_link)
            shows.add(show)

        log.debug("{0:d} show(s) are found".format(len(shows)))

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

            # log.debug('search_title: {0}; search_season: {1}; search_episode: {2}'.format(
            #     search_title, search_season, search_episode))

            for show in shows:
                if search_title not in show.titles:
                    continue

                try:
                    category_response = task.requests.get(show.url)
                except requests.RequestException as e:
                    log.error("Error while fetching page: {0}".format(e))
                    continue
                category_html = category_response.content

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
    plugin.register(LostFilmSearch, 'lostfilm_search', groups=['search'], api_ver=2)
