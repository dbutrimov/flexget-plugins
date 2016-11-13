# -*- coding: utf-8 -*-
from __future__ import unicode_literals, division, absolute_import
from builtins import *  # pylint: disable=unused-import, redefined-builtin

import re
import urllib


host_prefix = 'http://baibako.tv/'
host_regexp = re.compile(r'^https?://(?:www\.)?baibako\.tv.*$', flags=re.IGNORECASE)


def add_host_if_need(url):
    if not host_regexp.match(url):
        url = urllib.parse.urljoin(host_prefix, url)
    return url
