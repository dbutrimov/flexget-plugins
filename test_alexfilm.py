import os
import unittest
import yaml

import alexfilm


class TestAlexFilm(unittest.TestCase):
    def setUp(self):
        with open("test_config.yml", 'r') as stream:
            config = yaml.load(stream)
            self._username = config['secrets']['alexfilm']['username']
            self._password = config['secrets']['alexfilm']['password']

    def test_auth(self):
        auth_handler = alexfilm.AlexFilmAuth(self._username, self._password)
        print(auth_handler.cookies_)

        self.assertRaises(Exception)


if __name__ == '__main__':
    unittest.main()
