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

log = logging.getLogger('newstudio')

viewtopic_url_regexp = re.compile(r'^https?://(?:www\.)?newstudio\.tv/viewtopic\.php\?t=(\d+).*$', flags=re.IGNORECASE)
download_url_regexp = re.compile(r'^(?:.*)download.php\?id=(\d+)$', flags=re.IGNORECASE)

ep_regexp = re.compile(r"\([Сс]езон\s+(\d+)\W+[Cс]ерия\s+(\d+)\)", flags=re.IGNORECASE)

host_prefix = 'http://newstudio.tv/'
host_regexp = re.compile(r'^https?://(?:www\.)?newstudio\.tv.*$', flags=re.IGNORECASE)


class NewStudioShow(object):
    titles = []
    url = ''

    def __init__(self, titles, url):
        self.titles = titles
        self.url = url


class NewStudioUrlRewrite(object):
    def add_host_if_need(self, url):
        if not host_regexp.match(url):
            url = urllib.parse.urljoin(host_prefix, url)
        return url

    def url_rewritable(self, task, entry):
        viewtopic_url = entry['url']
        return viewtopic_url_regexp.match(viewtopic_url)

    def url_rewrite(self, task, entry):
        viewtopic_url = entry['url']

        try:
            viewtopic_response = task.requests.get(viewtopic_url)
        except requests.RequestException as e:
            reject_reason = "Error while fetching page: {0}".format(e)
            log.error(reject_reason)
            entry.reject(reject_reason)
            return False
        viewtopic_html = viewtopic_response.content

        viewtopic_soup = BeautifulSoup(viewtopic_html, 'html.parser')
        download_node = viewtopic_soup.find('a', href=download_url_regexp)
        if download_node:
            torrent_url = download_node.get('href')
            torrent_url = self.add_host_if_need(torrent_url)
            entry['url'] = torrent_url
            return True

        reject_reason = "Torrent link was not detected for `{0}`".format(viewtopic_url)
        log.error(reject_reason)
        entry.reject(reject_reason)
        return False

    def search(self, task, entry, config=None):
        entries = set()

        serials_url = 'http://newstudio.tv/'

        log.debug("Fetching serials page `{0}`...".format(serials_url))

        try:
            serials_response = task.requests.get(serials_url)
        except requests.RequestException as e:
            log.error("Error while fetching page: {0}".format(e))
            return None
        serials_html = serials_response.content

        log.debug("Parsing serials page `{0}`...".format(serials_url))

        serials_tree = BeautifulSoup(serials_html, 'html.parser')
        accordion_node = serials_tree.find('div', class_='accordion', id='serialist')
        if not accordion_node:
            log.error("Error while parsing serials page: node <div class=`accordion` id=`serialist`> are not found")
            return None

        shows = set()
        inner_nodes = accordion_node.find_all('div', class_='accordion-inner')
        for inner_node in inner_nodes:
            link_nodes = inner_node.find_all('a')
            for link_node in link_nodes:
                title = link_node.text
                viewforum_link = link_node.get('href')
                viewforum_link = self.add_host_if_need(viewforum_link)

                show = NewStudioShow([title], viewforum_link)
                shows.add(show)

        log.debug("{0:d} shows are found".format(len(shows)))

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

            # log.debug('search_title: {0}; search_season: {1}; search_episode: {2}'.format(
            #     search_title, search_season, search_episode))

            for show in shows:
                if search_title not in show.titles:
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
                        continue
                    viewforum_html = viewforum_response.content

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
                                page_link = self.add_host_if_need(page_link)
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
                        torrent_url = self.add_host_if_need(torrent_url)

                        entry = Entry()
                        entry['title'] = "{0} / s{1:02d}e{2:02d} / {3}".format(search_title, season, episode, quality)
                        entry['url'] = torrent_url

                        entries.add(entry)

        return entries


@event('plugin.register')
def register_plugin():
    plugin.register(NewStudioUrlRewrite, 'newstudio', groups=['urlrewriter', 'search'], api_ver=2)
