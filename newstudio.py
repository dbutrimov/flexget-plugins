# -*- coding: utf-8 -*-
from __future__ import unicode_literals, division, absolute_import
from builtins import *  # pylint: disable=unused-import, redefined-builtin

import os
import importlib.util
import re
from time import sleep
from bs4 import BeautifulSoup

import logging

from flexget import plugin
from flexget.event import event
from flexget.utils import requests


dir_path = os.path.dirname(os.path.abspath(__file__))

module_path = os.path.join(dir_path, 'newstudio_utils.py')
spec = importlib.util.spec_from_file_location('newstudio_utils', module_path)
newstudio_utils = importlib.util.module_from_spec(spec)
spec.loader.exec_module(newstudio_utils)


log = logging.getLogger('newstudio')

viewtopic_url_regexp = re.compile(r'^https?://(?:www\.)?newstudio\.tv/viewtopic\.php\?t=(\d+).*$', flags=re.IGNORECASE)
download_url_regexp = re.compile(r'^(?:.*)download.php\?id=(\d+)$', flags=re.IGNORECASE)


class NewStudioUrlRewrite(object):

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
            sleep(3)
            return False
        viewtopic_html = viewtopic_response.content
        sleep(3)

        viewtopic_soup = BeautifulSoup(viewtopic_html, 'html.parser')
        download_node = viewtopic_soup.find('a', href=download_url_regexp)
        if download_node:
            torrent_url = download_node.get('href')
            torrent_url = newstudio_utils.add_host_if_need(torrent_url)
            entry['url'] = torrent_url
            return True

        reject_reason = "Torrent link was not detected for `{0}`".format(viewtopic_url)
        log.error(reject_reason)
        entry.reject(reject_reason)
        return False


@event('plugin.register')
def register_plugin():
    plugin.register(NewStudioUrlRewrite, 'newstudio', groups=['urlrewriter'], api_ver=2)
