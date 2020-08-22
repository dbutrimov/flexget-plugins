# -*- coding: utf-8 -*-

import requests

TORRENT_CONTENT_TYPE = 'application/x-bittorrent'


def is_torrent(content_type: str) -> bool:
    return content_type.lower().startswith(TORRENT_CONTENT_TYPE)


def raise_not_torrent(response: requests.Response) -> None:
    content_type = response.headers['Content-Type']
    if is_torrent(content_type):
        return

    raise ValueError('Invalid content type: "{0}". Expected: "{1}"'.format(content_type, TORRENT_CONTENT_TYPE))
