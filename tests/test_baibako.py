# -*- coding: utf-8 -*-

import unittest

import requests
import yaml

from . import baibako


class TestBaibako(unittest.TestCase):
    def setUp(self):
        with open("test_config.yml", 'r') as stream:
            config = yaml.safe_load(stream)
            config = config['secrets']['baibako']

            self._username = config['username']
            self._password = config['password']

            self._requests = requests.Session()
            self._requests.headers.update({
                'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/45.0.2454.85 Safari/537.36'
            })

            self._auth = baibako.BaibakoAuth(self._username, self._password)
            self._requests.auth = self._auth

    def tearDown(self):
        self._requests.close()

    def test_forums(self):
        forums = baibako.Baibako.get_forums(self._requests)
        for forum in forums:
            print(u"[{0}] {1}".format(forum.id, forum.title))

        self.assertRaises(Exception)

    def test_forum_topics(self):
        topics = baibako.Baibako.get_forum_topics(1, 'all', self._requests)
        for topic in topics:
            print(u"[{0}] {1}".format(topic.id, topic.title))

            try:
                topic_info = baibako.BaibakoParser.parse_topic_title(topic.title)
                print(u"{0} / {1} / {2} / {3}".format(
                    topic_info.title,
                    topic_info.alternative_titles[0],
                    topic_info.get_episode_id(),
                    topic_info.quality
                ))
            except Exception as e:
                print(u"\033[91m[ERROR]\033[0m {0}".format(e))

    def test_download_torrent(self):
        info_hash = baibako.Baibako.get_info_hash(self._requests, 36068)
        self.assertEqual(len(info_hash), 40, "The hash has invalid length: {0}".format(info_hash))


if __name__ == '__main__':
    unittest.main()
