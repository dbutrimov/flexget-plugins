from __future__ import unicode_literals, division, absolute_import

from lxml import html
import re

import logging

from flexget import plugin
from flexget.event import event
from flexget.utils import requests

log = logging.getLogger('lostfilm')

download_url_regexp = re.compile(r'^https?://(www\.)?lostfilm\.tv/download\.php\?id=\d+.*$', flags=re.IGNORECASE)
details_url_regexp = re.compile(r'^https?://(www\.)?lostfilm\.tv/details\.php\?id=\d+.*$', flags=re.IGNORECASE)
replace_download_url_regexp = re.compile(r'/download\.php', flags=re.IGNORECASE)

onclick_regexp = re.compile(r'\(\\?\'(.+?)\\?\',\s*\\?\'(.+?)\\?\',\s*\\?\'(.+?)\\?\'\)', flags=re.IGNORECASE)
replace_location_regexp = re.compile(r'location\.replace\("(.+?)"\);', flags=re.IGNORECASE)


class LostFilmUrlRewrite(object):
    """
    LostFilm urlrewriter.

    Example::

      lostfilm:
        quality: sd
    """

    def url_rewritable(self, task, entry):
        url = entry['url']
        if download_url_regexp.search(url):
            return True
        if details_url_regexp.search(url):
            return True

        log.warning('invalid url: %s' % url)
        return False

    def url_rewrite(self, task, entry):
        details_url = entry['url']

        log.verbose('1. Start with url `%s`...' % details_url)

        # Convert download url to details if needed
        if download_url_regexp.search(details_url):
            new_url = replace_download_url_regexp.sub('/details.php', details_url)
            details_url = new_url
            log.verbose('1.1. Rewrite url to `%s`' % details_url)

        log.verbose('2. Download details page `%s`...' % details_url)

        try:
            details_response = task.requests.get(details_url)
        except requests.RequestException as e:
            log.error('Error while fetching page: %s' % e)
            entry['url'] = None
            return
        details_html = str(details_response.content)

        log.verbose('3. Parse details page `%s`...' % details_url)
        log.verbose('3.1. Find <div class="mid"> ...')

        details_tree = html.fromstring(details_html)
        mid_nodes = details_tree.xpath('//div[@class="mid"]')
        if len(mid_nodes) <= 0:
            log.error('len(mid_nodes) <= 0')
            entry['url'] = None
            return
        mid_node = mid_nodes[0]

        log.verbose('3.2. Find <a class="a_download"> ...')

        onclick_nodes = mid_node.xpath('.//a[@class="a_download" and starts-with(@onclick, "ShowAllReleases")]/@onclick')
        if len(onclick_nodes) <= 0:
            log.error('len(onclick_nodes) <= 0')
            entry['url'] = None
            return
        onclick_node = onclick_nodes[0]

        log.verbose('3.3. Parse `onclick` parameters <a class="a_download"> ...')

        onclick_match = onclick_regexp.search(onclick_node)
        if not onclick_match:
            log.error('not onclick_match')
            entry['url'] = None
            return
        category = onclick_match.group(1)
        season = onclick_match.group(2)
        episode = onclick_match.group(3)
        torrents_url = 'http://www.lostfilm.tv/nrdr2.php?c=' + category + '&s=' + season + '&e=' + episode

        log.verbose('4. Download torrents page `%s`...' % torrents_url)

        try:
            torrents_response = task.requests.get(torrents_url)
        except requests.RequestException as e:
            log.error('Error while fetching page: %s' % e)
            entry['url'] = None
            return
        torrents_html = str(torrents_response.content)

        replace_location_match = replace_location_regexp.search(torrents_html)
        if replace_location_match:
            replace_location_url = replace_location_match.group(1)

            log.verbose('4.1. Redirect to `%s`...' % replace_location_url)

            try:
                torrents_response = task.requests.get(replace_location_url)
            except requests.RequestException as e:
                log.error('Error while fetching page: %s' % e)
                entry['url'] = None
                return
            torrents_html = str(torrents_response.content)

        log.verbose('5. Parse torrent links ...')

        table_tree = html.fromstring(torrents_html)
        table_nodes = table_tree.xpath('//table')
        for table_node in table_nodes:
            torrent_links = table_node.xpath('.//a/@href')
            link_count = len(torrent_links)
            if link_count > 0:
                torrent_link = torrent_links[0]
                entry['url'] = torrent_link
                log.verbose('Field `%s` is now `%s`' % ('url', torrent_link))


@event('plugin.register')
def register_plugin():
    plugin.register(LostFilmUrlRewrite, 'lostfilm', groups=['urlrewriter'], api_ver=2)
