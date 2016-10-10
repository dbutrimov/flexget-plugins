from __future__ import unicode_literals, division, absolute_import
from builtins import *  # pylint: disable=unused-import, redefined-builtin

import re
from bs4 import BeautifulSoup

import logging

from flexget import plugin
from flexget.event import event
from flexget.utils import requests

log = logging.getLogger('lostfilm')

download_url_regexp = re.compile(r'^https?://(?:www\.)?lostfilm\.tv/download\.php\?id=(\d+).*$', flags=re.IGNORECASE)
details_url_regexp = re.compile(r'^https?://(?:www\.)?lostfilm\.tv/details\.php\?id=(\d+).*$', flags=re.IGNORECASE)
replace_download_url_regexp = re.compile(r'/download\.php', flags=re.IGNORECASE)

show_all_releases_regexp = re.compile(
    r'ShowAllReleases\(\\?[\'"](.+?)\\?[\'"],\s*\\?[\'"](.+?)\\?[\'"],\s*\\?[\'"](.+?)\\?[\'"]\)',
    flags=re.IGNORECASE)
replace_location_regexp = re.compile(r'location\.replace\("(.+?)"\);', flags=re.IGNORECASE)


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

    def on_task_start(self, task, config):
        if not isinstance(config, dict):
            log.verbose('Config was not determined - use default.')
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

        # log.verbose('1. Start with url `%s`...' % details_url)

        # Convert download url to details if needed
        if download_url_regexp.match(details_url):
            new_url = replace_download_url_regexp.sub('/details.php', details_url)
            details_url = new_url
            # log.verbose('1.1. Rewrite url to `%s`' % details_url)

        # log.verbose('2. Download details page `%s`...' % details_url)

        try:
            details_response = task.requests.get(details_url)
        except requests.RequestException as e:
            log.error('Error while fetching page: %s' % e)
            entry['url'] = None
            return False
        details_html = details_response.text

        # log.verbose('3. Parse details page `%s`...' % details_url)
        # log.verbose('3.1. Find <div class="mid"> ...')

        details_tree = BeautifulSoup(details_html, 'html.parser')
        mid_node = details_tree.find('div', class_='mid')
        if not mid_node:
            log.error('not mid_node')
            entry['url'] = None
            return False

        # log.verbose('3.2. Find <a class="a_download"> ...')

        onclick_node = mid_node.find('a', class_='a_download', onclick=show_all_releases_regexp)
        if not onclick_node:
            log.error('not onclick_node')
            entry['url'] = None
            return False

        # log.verbose('3.3. Parse `onclick` parameters <a class="a_download"> ...')

        onclick_match = show_all_releases_regexp.search(onclick_node.get('onclick'))
        if not onclick_match:
            log.error('not onclick_match')
            entry['url'] = None
            return False
        category = onclick_match.group(1)
        season = onclick_match.group(2)
        episode = onclick_match.group(3)
        torrents_url = 'http://www.lostfilm.tv/nrdr2.php?c=' + category + '&s=' + season + '&e=' + episode

        # log.verbose('4. Download torrents page `%s`...' % torrents_url)

        try:
            torrents_response = task.requests.get(torrents_url)
        except requests.RequestException as e:
            log.error('Error while fetching page: %s' % e)
            entry['url'] = None
            return False
        torrents_html = torrents_response.text

        replace_location_match = replace_location_regexp.search(torrents_html)
        if replace_location_match:
            replace_location_url = replace_location_match.group(1)

            # log.verbose('4.1. Redirect to `%s`...' % replace_location_url)

            try:
                torrents_response = task.requests.get(replace_location_url)
            except requests.RequestException as e:
                log.error('Error while fetching page: %s' % e)
                entry['url'] = None
                return False
            torrents_html = torrents_response.text

        text_pattern = self.config.get('regexp')
        if not isinstance(text_pattern, str):
            text_pattern = '.*'
        text_regexp = re.compile(text_pattern, flags=re.IGNORECASE)

        # log.verbose('5. Parse torrent links ...')

        torrents_tree = BeautifulSoup(torrents_html, 'html.parser')
        table_nodes = torrents_tree.find_all('table')
        for table_node in table_nodes:
            link_node = table_node.find('a')
            if link_node:
                torrent_link = link_node.get('href')
                description_text = link_node.text
                if text_regexp.match(description_text):
                    # log.verbose('5.1. Direct link are detected! [ regexp: `%s`, description: `%s` ]' %
                    #             (text_pattern, description_text))
                    entry['url'] = torrent_link
                    log.verbose('Field `%s` is now `%s`' % ('url', torrent_link))
                    return True

        log.error('Direct link are not received :(')
        entry['url'] = None
        return False


@event('plugin.register')
def register_plugin():
    plugin.register(LostFilmUrlRewrite, 'lostfilm', groups=['urlrewriter'], api_ver=2)
