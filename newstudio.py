from __future__ import unicode_literals, division, absolute_import
from builtins import *  # pylint: disable=unused-import, redefined-builtin

import re
from bs4 import BeautifulSoup

import logging

from flexget import plugin
from flexget.event import event
from flexget.utils import requests

log = logging.getLogger('newstudio')

viewtopic_url_regexp = re.compile(r'^https?://(?:www\.)?newstudio\.tv/viewtopic\.php\?t=(\d+).*$', flags=re.IGNORECASE)
download_url_regexp = re.compile(r'^download.php\?id=(\d+)$', flags=re.IGNORECASE)


class NewStudioUrlRewrite(object):
    def url_rewritable(self, task, entry):
        viewtopic_url = entry['url']
        return viewtopic_url_regexp.match(viewtopic_url)

    def url_rewrite(self, task, entry):
        viewtopic_url = entry['url']

        try:
            viewtopic_response = task.requests.get(viewtopic_url)
        except requests.RequestException as e:
            log.error('Error while fetching page: %s' % e)
            entry['url'] = None
            return False
        viewtopic_html = viewtopic_response.text

        viewtopic_soup = BeautifulSoup(viewtopic_html, 'html.parser')
        download_node = viewtopic_soup.find('a', href=download_url_regexp)
        if download_node:
            torrent_link = download_node.get('href')
            torrent_link = 'http://newstudio.tv/' + torrent_link
            entry['url'] = torrent_link
            log.verbose('Field `%s` is now `%s`' % ('url', torrent_link))
            return True

        log.error('Torrent link are not detected for `%s`' % viewtopic_url)
        entry['url'] = None
        return False


@event('plugin.register')
def register_plugin():
    plugin.register(NewStudioUrlRewrite, 'newstudio', groups=['urlrewriter'], api_ver=2)
