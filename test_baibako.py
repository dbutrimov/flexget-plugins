import requests
import unittest
import yaml

import baibako


class TestBaibako(unittest.TestCase):
    def setUp(self):
        with open("test_config.yml", 'r') as stream:
            config = yaml.load(stream)
            self._username = config['secrets']['baibako']['username']
            self._password = config['secrets']['baibako']['password']

    def test_auth(self):
        auth_handler = baibako.BaibakoAuth(self._username, self._password)
        print(auth_handler.cookies_)

        self.assertRaises(Exception)
        return auth_handler

    def test_shows_parsing(self):
        response = requests.get('http://baibako.tv/serials.php')
        html = response.text
        entries = baibako.BaibakoParser.parse_shows_page(html)
        for entry in entries:
            print(entry)

        self.assertRaises(Exception)

    def test_episodes_parsing(self):
        auth_handler = self.test_auth()
        response = requests.get('http://baibako.tv/serial.php?id=472&tab=hd720', auth=auth_handler)
        html = response.text
        entries = baibako.BaibakoParser.parse_episodes_page(html)
        for entry in entries:
            print(entry)

        self.assertRaises(Exception)


if __name__ == '__main__':
    unittest.main()
