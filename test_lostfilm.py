import requests
import unittest
try:
    from urllib.parse import urljoin
except ImportError:
    from urlparse import urljoin
import yaml

import lostfilm


class TestLostFilm(unittest.TestCase):
    def setUp(self):
        with open("test_config.yml", 'r') as stream:
            config = yaml.load(stream)
            self._username = config['secrets']['lostfilm']['username']
            self._password = config['secrets']['lostfilm']['password']

    def test_auth(self):
        auth_handler = lostfilm.LostFilmAuth(self._username, self._password)
        print(auth_handler.cookies_)

        self.assertRaises(Exception)

    def test_shows_parsing(self):
        step = 10
        total = 0

        total_shows = list()
        while True:
            payload = {
                'act': 'serial',
                'type': 'search',
                'o': total,
                's': 2,
                't': 0
            }

            count = 0
            try:
                response = lostfilm.LostFilmApi.requests_post(requests, payload)
            except Exception as e:
                print(e)
            else:
                try:
                    shows = lostfilm.LostFilmParser.parse_shows_page(response.text)
                except Exception as e:
                    print(e)
                else:
                    if shows:
                        count = len(shows)
                        for show in shows:
                            url = show['url']
                            url = urljoin(response.url, url)
                            url = urljoin(url + '//', 'seasons')
                            show['url'] = url
                            print(' / '.join(x for x in show['titles']))
                            total_shows.append(show)

            total += count
            if count < step:
                break
        print(len(total_shows))

        self.assertRaises(Exception)

    def test_episodes_parsing(self):
        response = requests.get('http://www.lostfilm.tv/series/Timeless/seasons/')
        html = response.content
        entries = lostfilm.LostFilmParser.parse_episodes_page(html)
        for entry in entries:
            print(entry)

        self.assertRaises(Exception)

    def test_episode_parsing(self):
        response = requests.get('http://www.lostfilm.tv/series/Timeless/season_1/episode_14/')
        html = response.content
        entry = lostfilm.LostFilmParser.parse_episode_page(html)
        print(entry)

        self.assertRaises(Exception)

    def test_torrents_parsing(self):
        response = requests.get('http://retre.org/v3/?c=291&s=1&e=14&u=821592&h=3616626b4f0c96f12abf700a9b8d9b5e&n=1')
        html = response.content
        entries = lostfilm.LostFilmParser.parse_torrents_page(html)
        for entry in entries:
            print(entry)

        self.assertRaises(Exception)


if __name__ == '__main__':
    unittest.main()
