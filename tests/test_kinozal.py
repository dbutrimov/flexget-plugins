# -*- coding: utf-8 -*-

import unittest

import requests
import yaml

from plugins.kinozal import KinozalReAuthAdapter
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

            adapter = KinozalReAuthAdapter(self._auth)
            self._requests.mount('https://', adapter)
            self._requests.mount('http://', adapter)

    def tearDown(self):
        self._requests.close()

    def test_search(self):
        search_result = kinozal.Kinozal.search(self._requests, "game of thrones")
        for entry in search_result:
            print(u"[{0}] {1} -> {2}".format(entry.id, entry.title, entry.url))

    def test_info_hash(self):
        topic_id = 1947026
        info_hash = kinozal.Kinozal.get_info_hash(self._requests, topic_id)
        print(u"{0} -> {1}".format(topic_id, info_hash))
        self.assertEqual(len(info_hash), 40, "The hash has invalid length: {0}".format(info_hash))

    def test_download(self):
        topic_id = 1947026
        url = 'https://kinozal.tv/download.php?id={0}'.format(topic_id)
        response = self._requests.get(url)
        response.raise_for_status()
        ContentType.raise_not_torrent(response)

        content_type = response.headers['Content-Type'].lower()
        print(u"{0} -> {1}".format(topic_id, content_type))

        self.assertEqual(content_type, "application/x-bittorrent",
                         "The response has invalid content type: {0}".format(content_type))


if __name__ == '__main__':
    unittest.main()
