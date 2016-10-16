# -*- coding: utf-8 -*-
from __future__ import unicode_literals, division, absolute_import
from builtins import *  # pylint: disable=unused-import, redefined-builtin

import re
from bs4 import BeautifulSoup
import urllib

import logging

from flexget import plugin
from flexget.event import event
from flexget.utils import requests

log = logging.getLogger('newstudio')

viewtopic_url_regexp = re.compile(r'^https?://(?:www\.)?newstudio\.tv/viewtopic\.php\?t=(\d+).*$', flags=re.IGNORECASE)
download_url_regexp = re.compile(r'^(?:.*)download.php\?id=(\d+)$', flags=re.IGNORECASE)

host_prefix = 'http://newstudio.tv/'
host_regexp = re.compile(r'^https?://(?:www\.)?newstudio\.tv.*$', flags=re.IGNORECASE)


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
        viewtopic_html = viewtopic_response.text

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


@event('plugin.register')
def register_plugin():
    plugin.register(NewStudioUrlRewrite, 'newstudio', groups=['urlrewriter'], api_ver=2)
