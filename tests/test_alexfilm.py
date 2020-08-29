# -*- coding: utf-8 -*-

import unittest

import requests
import yaml

from .context import alexfilm, raise_not_torrent


class TestAlexFilm(unittest.TestCase):
    def setUp(self):
        with open("test_config.yml", 'r') as stream:
            config = yaml.safe_load(stream)
            config = config['secrets']['alexfilm']

            self._username = config['username']
            self._password = config['password']

            self._auth = alexfilm.AlexFilmAuth(self._username, self._password)
            self._requests = requests.Session()
            self._requests.auth = self._auth

    def tearDown(self):
        self._requests.close()

    def test_magnet(self):
        magnet = alexfilm.AlexFilm.get_marget(self._requests, 1814)
        print(magnet)

    def test_download_torrent(self):
        download_url = alexfilm.AlexFilm.get_download_url(self._requests, 1814)
        print(download_url)

        response = self._requests.get(download_url)
        response.raise_for_status()
        raise_not_torrent(response)

        content_type = response.headers['Content-Type']
        print(content_type)


if __name__ == '__main__':
    unittest.main()
