# -*- coding: utf-8 -*-
from __future__ import unicode_literals, division, absolute_import
from builtins import *  # pylint: disable=unused-import, redefined-builtin

import re
import logging
import urllib
from bs4 import BeautifulSoup
from time import sleep

from flexget import plugin
from flexget.event import event
from flexget.utils import requests


plugin_name = 'alexfilm'

log = logging.getLogger(plugin_name)

topic_url_regexp = re.compile(r'^https?://(?:www\.)?alexfilm\.cc/viewtopic\.php\?t=(\d+).*$', flags=re.IGNORECASE)
download_url_regexp = re.compile(r'dl\.php\?id=(\d+)', flags=re.IGNORECASE)

host_prefix = 'http://alexfilm.cc/'
host_regexp = re.compile(r'^https?://(?:www\.)?alexfilm\.cc.*$', flags=re.IGNORECASE)


class AlexFilmUrlRewrite(object):
    """AlexFilm urlrewriter."""

    def add_host_if_need(self, url):
        if not host_regexp.match(url):
            url = urllib.parse.urljoin(host_prefix, url)
        return url

    def url_rewritable(self, task, entry):
        url = entry['url']
        return topic_url_regexp.match(url)

    def url_rewrite(self, task, entry):
        topic_url = entry['url']

        try:
            topic_response = task.requests.get(topic_url)
        except requests.RequestException as e:
            reject_reason = "Error while fetching page: {0}".format(e)
            log.error(reject_reason)
            entry.reject(reject_reason)
            sleep(3)
            return False
        topic_html = topic_response.content
        sleep(3)

        topic_tree = BeautifulSoup(topic_html, 'html.parser')
        download_node = topic_tree.find('a', href=download_url_regexp)
        if not download_node:
            reject_reason = "Error while parsing topic page: download node are not found"
            log.error(reject_reason)
            entry.reject(reject_reason)
            return False

        download_url = download_node.get('href')
        download_url = self.add_host_if_need(download_url)

        entry['url'] = download_url
        return True


@event('plugin.register')
def register_plugin():
    plugin.register(AlexFilmUrlRewrite, plugin_name, groups=['urlrewriter'], api_ver=2)
