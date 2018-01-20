import requests
import unittest
import yaml

import lostfilm


class TestLostFilm(unittest.TestCase):
    _requests = None

    def setUp(self):
        with open("test_config.yml", 'r') as stream:
            config = yaml.load(stream)

            requests_ = TestLostFilm._requests
            if not requests_:
                lostfilm_config = config['secrets']['lostfilm']
                username = lostfilm_config['username']
                password = lostfilm_config['password']

                requests_ = requests.session()
                requests_.auth = lostfilm.LostFilmAuth(username, password)

                TestLostFilm._requests = requests_

    def test_shows(self):
        shows = lostfilm.LostFilm.get_shows(TestLostFilm._requests)
        print('{0} show(s)'.format(len(shows)))
        for show in shows:
            print(u"[{0}, {1}] {2}".format(show.id, show.slug, show.title))

        self.assertRaises(Exception)

    def test_episodes(self):
        episodes = lostfilm.LostFilm.get_show_episodes('Godless', TestLostFilm._requests)
        for episode in episodes:
            print(u"[s{0:02d}e{1:02d}] {2}".format(episode.season, episode.episode, episode.title))

        self.assertRaises(Exception)

    def test_episode_torrents(self):
        torrents = lostfilm.LostFilm.get_episode_torrents(351, 1, 1, TestLostFilm._requests)
        for torrent in torrents:
            print(u"[{0}] {1} - {2}".format(torrent.label, torrent.title, torrent.url))

        self.assertRaises(Exception)


if __name__ == '__main__':
    unittest.main()
