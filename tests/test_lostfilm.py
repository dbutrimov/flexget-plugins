# -*- coding: utf-8 -*-

from typing import Optional

import requests
import unittest
import yaml

from . import lostfilm, flaresolverr, clearance, ContentType


class TestLostFilm(unittest.TestCase):
    def setUp(self):
        with open("test_config.yml", 'r') as stream:
            config = yaml.safe_load(stream)
            clearance_config = config.get('clearance')
            flaresolverr_config = config.get('flaresolverr')
            config = config['secrets']['lostfilm']

            self._username = config['username']
            self._password = config['password']

            if clearance_config:
                endpoint = clearance_config \
                    if isinstance(clearance_config, str) \
                    else clearance_config.get('endpoint')
                self._requests = clearance.Clearance.create_clearance(endpoint)
            elif flaresolverr_config:
                endpoint = flaresolverr_config \
                    if isinstance(flaresolverr_config, str) \
                    else flaresolverr_config.get('endpoint')
                self._requests = flaresolverr.FlareSolverr.create_solverr(endpoint)
            else:
                self._requests = requests.Session()

            self._auth = lostfilm.LostFilmAuth(self._username, self._password, requests=self._requests)
            self._requests.auth = self._auth

    def tearDown(self):
        del self._requests
        del self._auth

    def test_shows(self):
        shows = lostfilm.LostFilm.get_shows(self._requests)
        print('{0} show(s)'.format(len(shows)))
        for show in shows:
            print(u"[{0}, {1}] {2}".format(show.id, show.slug, show.title))

        self.assertRaises(Exception)

    def test_episode(self):
        episode = lostfilm.LostFilm.get_show_episode(self._requests, 'The_Rookie', 5, 17)
        print(u"[{0} - s{1:02d}e{2:02d}] {3}".format(episode.show_id, episode.season, episode.episode, episode.title))

        torrents = lostfilm.LostFilm.get_episode_torrents(
            self._requests, episode.show_id, episode.season, episode.episode)
        for torrent in torrents:
            print(u"[{0}] {1} - {2}".format(torrent.label, torrent.title, torrent.url))

        self.assertRaises(Exception)

    def test_episodes(self):
        episodes = lostfilm.LostFilm.get_show_episodes(self._requests, 'Godless')
        for episode in episodes:
            print(u"[{0} - s{1:02d}e{2:02d}] {3}".format(
                episode.show_id, episode.season, episode.episode, episode.title))

        self.assertRaises(Exception)

    def test_episode_torrents(self):
        torrents = lostfilm.LostFilm.get_episode_torrents(self._requests, 384, 5, 17)
        for torrent in torrents:
            print(u"[{0}] {1} - {2}".format(torrent.label, torrent.title, torrent.url))

            response = self._requests.get(torrent.url)
            response.raise_for_status()
            ContentType.raise_not_torrent(response)

            content_type = response.headers['Content-Type']
            print(content_type)

        self.assertRaises(Exception)


if __name__ == '__main__':
    unittest.main()
