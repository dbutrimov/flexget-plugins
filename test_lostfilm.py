import requests
import unittest
import yaml

import lostfilm


class TestLostFilm(unittest.TestCase):
    _requests = None

    def setUp(self):
        with open("test_config.yml", 'r') as stream:
            config = yaml.safe_load(stream)

            requests_ = TestLostFilm._requests
            if not requests_:
                lostfilm_config = config['secrets']['lostfilm']
                username = lostfilm_config['username']
                password = lostfilm_config['password']

                requests_ = requests.Session()
                requests_.auth = lostfilm.LostFilmAuth(username, password)

                TestLostFilm._requests = requests_

    def test_shows(self):
        shows = lostfilm.LostFilm.get_shows(TestLostFilm._requests)
        print('{0} show(s)'.format(len(shows)))
        for show in shows:
            print(u"[{0}, {1}] {2}".format(show.id, show.slug, show.title))

        self.assertRaises(Exception)

    def test_episode(self):
        episode = lostfilm.LostFilm.get_show_episode(TestLostFilm._requests, 'The_Blacklist', 5, 11)
        print(u"[{0} - s{1:02d}e{2:02d}] {3}".format(episode.show_id, episode.season, episode.episode, episode.title))

        torrents = lostfilm.LostFilm.get_episode_torrents(
            TestLostFilm._requests, episode.show_id, episode.season, episode.episode)
        for torrent in torrents:
            print(u"[{0}] {1} - {2}".format(torrent.label, torrent.title, torrent.url))

        self.assertRaises(Exception)

    def test_episodes(self):
        episodes = lostfilm.LostFilm.get_show_episodes(TestLostFilm._requests, 'Godless')
        for episode in episodes:
            print(u"[{0} - s{1:02d}e{2:02d}] {3}".format(
                episode.show_id, episode.season, episode.episode, episode.title))

        self.assertRaises(Exception)

    def test_episode_torrents(self):
        torrents = lostfilm.LostFilm.get_episode_torrents(TestLostFilm._requests, 412, 1, 5)
        for torrent in torrents:
            print(u"[{0}] {1} - {2}".format(torrent.label, torrent.title, torrent.url))

        self.assertRaises(Exception)


if __name__ == '__main__':
    unittest.main()
