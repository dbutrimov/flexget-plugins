# -*- coding: utf-8 -*-

import cgi
import json

from requests import Response
from sqlalchemy.types import TypeDecorator, VARCHAR


class JSONEncodedDict(TypeDecorator):
    """
    Represents an immutable structure as a json-encoded string.

    Usage:
        JSONEncodedDict(255)
    """

    impl = VARCHAR

    def process_bind_param(self, value, dialect):
        if value is not None:
            value = json.dumps(value)
        return value

    def process_result_value(self, value, dialect):
        if value is not None:
            value = json.loads(value)
        return value


class ContentType(object):
    TORRENT_CONTENT_TYPE = 'application/x-bittorrent'

    @staticmethod
    def is_torrent(content_type: str) -> bool:
        mimetype, options = cgi.parse_header(content_type)
        return mimetype.lower() == ContentType.TORRENT_CONTENT_TYPE

    @staticmethod
    def raise_not_torrent(response: Response) -> None:
        content_type = response.headers['Content-Type']
        if ContentType.is_torrent(content_type):
            return

        raise ValueError('Invalid content type: "{0}". Expected: "{1}"'.format(
            content_type, ContentType.TORRENT_CONTENT_TYPE))
