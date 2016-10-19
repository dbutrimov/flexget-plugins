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

log = logging.getLogger('lostfilm')

download_url_regexp = re.compile(r'^https?://(?:www\.)?lostfilm\.tv/download\.php\?id=(\d+).*$', flags=re.IGNORECASE)
details_url_regexp = re.compile(r'^https?://(?:www\.)?lostfilm\.tv/details\.php\?id=(\d+).*$', flags=re.IGNORECASE)
replace_download_url_regexp = re.compile(r'/download\.php', flags=re.IGNORECASE)

host_prefix = 'http://www.lostfilm.tv/'
host_regexp = re.compile(r'^https?://(?:www\.)?lostfilm\.tv.*$', flags=re.IGNORECASE)

show_all_releases_regexp = re.compile(
    r'ShowAllReleases\(\\?[\'"](.+?)\\?[\'"],\s*\\?[\'"](.+?)\\?[\'"],\s*\\?[\'"](.+?)\\?[\'"]\)',
    flags=re.IGNORECASE)
replace_location_regexp = re.compile(r'location\.replace\("(.+?)"\);', flags=re.IGNORECASE)


class LostFilmShow(object):
    titles = []
    url = ''

    def __init__(self, titles, url):
        self.titles = titles
        self.url = url


class LostFilmUrlRewrite(object):
    """
    LostFilm urlrewriter.

    Example::

      lostfilm:
        regexp: '1080p'
    """

    config = {}

    schema = {
        'type': 'object',
        'properties': {
            'regexp': {'type': 'string', 'format': 'regex'}
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
        if download_url_regexp.match(url):
            return True
        if details_url_regexp.match(url):
            return True

        return False

    def url_rewrite(self, task, entry):
        details_url = entry['url']

        log.debug("Starting with url `{0}`...".format(details_url))

        # Convert download url to details if needed
        if download_url_regexp.match(details_url):
            details_url = replace_download_url_regexp.sub('/details.php', details_url)
            log.debug("Rewrite url to `{0}`".format(details_url))

        log.debug("Fetching details page `{0}`...".format(details_url))

        try:
            details_response = task.requests.get(details_url)
        except requests.RequestException as e:
            reject_reason = "Error while fetching page: {0}".format(e)
            log.error(reject_reason)
            entry.reject(reject_reason)
            return False
        details_html = details_response.content

        log.debug("Parsing details page `{0}`...".format(details_url))

        details_tree = BeautifulSoup(details_html, 'html.parser')
        mid_node = details_tree.find('div', class_='mid')
        if not mid_node:
            reject_reason = "Error while parsing details page: node <div class=`mid`> are not found"
            log.error(reject_reason)
            entry.reject(reject_reason)
            return False

        onclick_node = mid_node.find('a', class_='a_download', onclick=show_all_releases_regexp)
        if not onclick_node:
            reject_reason = "Error while parsing details page: node <a class=`a_download`> are not found"
            log.error(reject_reason)
            entry.reject(reject_reason)
            return False

        onclick_match = show_all_releases_regexp.search(onclick_node.get('onclick'))
        if not onclick_match:
            reject_reason = "Error while parsing details page: " \
                            "node <a class=`a_download`> have invalid `onclick` attribute"
            log.error(reject_reason)
            entry.reject(reject_reason)
            return False
        category = onclick_match.group(1)
        season = onclick_match.group(2)
        episode = onclick_match.group(3)
        torrents_url = "http://www.lostfilm.tv/nrdr2.php?c={0}&s={1}&e={2}".format(category, season, episode)

        log.debug(u"Downloading torrents page `{0}`...".format(torrents_url))

        try:
            torrents_response = task.requests.get(torrents_url)
        except requests.RequestException as e:
            reject_reason = "Error while fetching page: {0}".format(e)
            log.error(reject_reason)
            entry.reject(reject_reason)
            return False
        torrents_html = torrents_response.content

        torrents_html_text = torrents_html.decode(torrents_response.encoding)
        replace_location_match = replace_location_regexp.search(torrents_html_text)
        if replace_location_match:
            replace_location_url = replace_location_match.group(1)

            log.debug("Redirecting to `{0}`...".format(replace_location_url))

            try:
                torrents_response = task.requests.get(replace_location_url)
            except requests.RequestException as e:
                reject_reason = "Error while fetching page: {0}".format(e)
                log.error(reject_reason)
                entry.reject(reject_reason)
                return False
            torrents_html = torrents_response.content

        text_pattern = self.config.get('regexp')
        if not isinstance(text_pattern, str):
            text_pattern = '.*'
        text_regexp = re.compile(text_pattern, flags=re.IGNORECASE)

        log.debug("Parsing torrent links...")

        torrents_tree = BeautifulSoup(torrents_html, 'html.parser')
        table_nodes = torrents_tree.find_all('table')
        for table_node in table_nodes:
            link_node = table_node.find('a')
            if link_node:
                torrent_link = link_node.get('href')
                description_text = link_node.get_text()
                if text_regexp.search(description_text):
                    log.debug("Torrent link was accepted! [ regexp: `{0}`, description: `{1}` ]".format(
                              text_pattern, description_text))
                    entry['url'] = torrent_link
                    return True
                else:
                    log.debug("Torrent link was rejected: [ regexp: `{0}`, description: `{1}` ]".format(
                              text_pattern, description_text))

        reject_reason = "Torrent link was not detected with regexp `{0}`".format(text_pattern)
        log.error(reject_reason)
        entry.reject(reject_reason)
        return False

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
            category_link = self.add_host_if_need(category_link)

            # log.debug("Serial `{0}` was added".format(" / ".join(titles)))
            show = LostFilmShow(titles, category_link)
            shows.add(show)

        log.debug("{0:d} show(s) are found".format(len(shows)))

        search_regexp = re.compile('^(.*?)\s*s(\d+?)e(\d+?)$', flags=re.IGNORECASE)

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

                row_nodes = mid_node.find_all('div', class_=re.compile('t_row.*', flags=re.IGNORECASE))
                for row_node in row_nodes:
                    ep_node = row_node.find('span', class_='micro')
                    if not ep_node:
                        continue

                    ep_regexp = re.compile("(\d+)\s+сезон\s+(\d+)\s+серия", flags=re.IGNORECASE)
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
                    details_url = self.add_host_if_need(details_url)

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
    plugin.register(LostFilmUrlRewrite, 'lostfilm', groups=['urlrewriter', 'search'], api_ver=2)
