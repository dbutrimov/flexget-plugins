# -*- coding: utf-8 -*-

import unittest

import requests
import yaml

from . import kinozal, ContentType


class TestKinozal(unittest.TestCase):
    def setUp(self):
        with open("test_config.yml", 'r') as stream:
            config = yaml.safe_load(stream)
            config = config['secrets']['kinozal']

            self._username = config['username']
            self._password = config['password']

            self._auth = kinozal.KinozalAuth(self._username, self._password)
            self._requests = requests.Session()
            self._requests.auth = self._auth

    def tearDown(self):
        self._requests.close()

    def test_search(self):
        search_result = kinozal.Kinozal.search(self._requests, "game of thrones")
        for entry in search_result:
            print(u"[{0}] {1} -> {2}".format(entry.id, entry.title, entry.url))

    def test_info_hash(self):
        info_hash = kinozal.Kinozal.get_info_hash(self._requests, 1791623)
        self.assertEqual(len(info_hash), 40, "The hash has invalid length: {0}".format(info_hash))

    def test_download(self):
        url = 'http://kinozal.tv/download.php?id={0}'.format(1791623)
        response = self._requests.get(url)
        response.raise_for_status()
        ContentType.raise_not_torrent(response)

        content_type = response.headers['Content-Type'].lower()
        self.assertEqual(content_type, "application/x-bittorrent",
                         "The response has invalid content type: {0}".format(content_type))


if __name__ == '__main__':
    unittest.main()
