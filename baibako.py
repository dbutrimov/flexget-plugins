# -*- coding: utf-8 -*-
from __future__ import unicode_literals, division, absolute_import
from builtins import *  # pylint: disable=unused-import, redefined-builtin

import re
import logging

from flexget import plugin
from flexget.event import event


log = logging.getLogger('baibako')

details_url_regexp = re.compile(r'^https?://(?:www\.)?baibako\.tv/details\.php\?id=(\d+).*$', flags=re.IGNORECASE)


class BaibakoUrlRewrite(object):
    """BaibaKo urlrewriter."""

    def url_rewritable(self, task, entry):
        url = entry['url']
        return details_url_regexp.match(url)

    def url_rewrite(self, task, entry):
        url = entry['url']
        url_match = details_url_regexp.search(url)
        if not url_match:
            reject_reason = "Url don't matched: {0}".format(url)
            log.verbose(reject_reason)
            # entry.reject(reject_reason)
            return False

        topic_id = url_match.group(1)
        url = 'http://baibako.tv/download.php?id={0}'.format(topic_id)
        entry['url'] = url
        return True


@event('plugin.register')
def register_plugin():
    plugin.register(BaibakoUrlRewrite, 'baibako', groups=['urlrewriter'], api_ver=2)
