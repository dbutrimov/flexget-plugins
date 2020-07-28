# -*- coding: utf-8 -*-

import requests


class ContentType(object):
    @staticmethod
    def is_torrent(content_type: str) -> bool:
        return content_type.lower().startswith('application/x-bittorrent')

    @staticmethod
    def raise_not_torrent(response: requests.Response) -> None:
        content_type = response.headers['Content-Type']
        if ContentType.is_torrent(content_type):
            return

        raise TypeError("It is not a torrent file")
