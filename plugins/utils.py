# -*- coding: utf-8 -*-

import cgi

import requests


class ContentType(object):
    TORRENT_CONTENT_TYPE = 'application/x-bittorrent'

    @staticmethod
    def is_torrent(content_type: str) -> bool:
        mimetype, options = cgi.parse_header(content_type)
        return mimetype.lower() == ContentType.TORRENT_CONTENT_TYPE

    @staticmethod
    def raise_not_torrent(response: requests.Response) -> None:
        content_type = response.headers['Content-Type']
        if ContentType.is_torrent(content_type):
            return

        raise ValueError('Invalid content type: "{0}". Expected: "{1}"'.format(
            content_type, ContentType.TORRENT_CONTENT_TYPE))
