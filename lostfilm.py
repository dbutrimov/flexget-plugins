# -*- coding: utf-8 -*-
from __future__ import unicode_literals, division, absolute_import
from builtins import *  # pylint: disable=unused-import, redefined-builtin

import re
from time import sleep
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

    config_ = {}

    schema = {
        'type': 'object',
        'properties': {
            'regexp': {'type': 'string', 'format': 'regex'}
        },
        'additionalProperties': False
    }

    def on_task_start(self, task, config):
        if not isinstance(config, dict):
            log.verbose("Config was not determined - use default.")
        else:
            self.config_ = config

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
            sleep(3)
            return False
        details_html = details_response.content
        sleep(3)

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
            sleep(3)
            return False
        torrents_html = torrents_response.content
        sleep(3)

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
                sleep(3)
                return False
            torrents_html = torrents_response.content
            sleep(3)

        text_pattern = self.config_.get('regexp', '.*')
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


@event('plugin.register')
def register_plugin():
    plugin.register(LostFilmUrlRewrite, 'lostfilm', groups=['urlrewriter'], api_ver=2)
