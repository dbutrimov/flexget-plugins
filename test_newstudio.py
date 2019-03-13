# -*- coding: utf-8 -*-
import unittest
import yaml
import requests

import newstudio


class TestNewStudio(unittest.TestCase):
    def setUp(self):
        with open("test_config.yml", 'r') as stream:
            config = yaml.load(stream)
            self._username = config['secrets']['newstudio']['username']
            self._password = config['secrets']['newstudio']['password']

            self._auth = newstudio.NewStudioAuth(self._username, self._password)
            self._requests = requests.session()
            self._requests.auth = self._auth

    def test_forums(self):
        forums = newstudio.NewStudio.get_forums(self._requests)
        for forum in forums:
            print(u"[{0}] {1}".format(forum.id, forum.title))

        self.assertRaises(Exception)

    def test_forum_topics(self):
        topics = newstudio.NewStudio.get_forum_topics(206, self._requests)
        print(len(topics))

        for topic in topics:
            try:
                topic_info = newstudio.NewStudioParser.parse_topic_title(topic.title)
            except newstudio.ParsingError as e:
                print(e.message)
            else:
                print(u"[{0}, {1}] {2} - ({3}, {4})".format(
                    topic.id, topic.download_id, topic_info.title,
                    topic_info.get_episode_id(), topic_info.quality))

        self.assertRaises(Exception)

    def test_title_parsing(self):
        titles = [
            u"И никого не стало (Сезон 1) / And Then There Were None (2015) HDTV 720p | Happy End",
            u"И никого не стало (Сезон 1, Серия 3) / And Then There Were None (2015) HDTV 720p | Happy End",
            u"И никого не стало (Сезон 1, Серия 3-10) / And Then There Were None (2015) HDTV 720p | Happy End"
        ]

        for title in titles:
            try:
                topic_info = newstudio.NewStudioParser.parse_topic_title(title)
            except newstudio.ParsingError as e:
                print(e.message)
            else:
                print(topic_info.get_episode_id())

        self.assertRaises(Exception)

    def test_torrent(self):
        download_url = newstudio.NewStudio.get_download_url(31460)
        print(download_url)

        self.assertRaises(Exception)


if __name__ == '__main__':
    unittest.main()
