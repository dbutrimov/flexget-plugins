# -*- coding: utf-8 -*-
import cgi
import unittest
import yaml
import requests

from .context import newstudio, raise_not_torrent
import urllib3


class TestNewStudio(unittest.TestCase):
    def setUp(self):
        with open("test_config.yml", 'r') as stream:
            config = yaml.safe_load(stream)
            config = config['secrets']['newstudio']

            self._username = config['username']
            self._password = config['password']

            self._auth = newstudio.NewStudioAuth(self._username, self._password)
            self._requests = requests.session()
            self._requests.auth = self._auth

    def tearDown(self):
        self._requests.close()

    def test_forums(self):
        forums = newstudio.NewStudio.get_forums(self._requests)
        for forum in forums:
            print(u"[{0}] {1}".format(forum.id, forum.title))

        self.assertRaises(Exception)

    def test_forum_topics(self):
        topics = newstudio.NewStudio.get_forum_topics(505, self._requests)
        print(len(topics))

        for topic in topics:
            print(u"[{0}, {1}] {2}".format(topic.id, topic.download_id, topic.title))

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

    def test_download_torrent(self):
        download_url = newstudio.NewStudio.get_download_url(36517)
        print(download_url)

        response = self._requests.get(download_url)
        response.raise_for_status()
        raise_not_torrent(response)

        content_type = response.headers['Content-Type']
        print(content_type)

    def test_filename(self):
        http = urllib3.PoolManager()
        url = "http://releases.ubuntu.com/18.04/ubuntu-18.04.3-desktop-amd64.iso.torrent?_ga=2.196584104.506460685.1574018110-1051848907.1572256016"
        response = http.request('GET', url)
        content_disposition = response.headers.get('Content-Disposition', '')
        _, params = cgi.parse_header(content_disposition)
        filename = params.get('filename')
        print(filename)
        # print(response.info().get_filename())


if __name__ == '__main__':
    unittest.main()
