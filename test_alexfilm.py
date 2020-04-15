import requests
import unittest
import yaml

import alexfilm


class TestAlexFilm(unittest.TestCase):
    _requests = None

    def setUp(self):
        with open("test_config.yml", 'r') as stream:
            config = yaml.safe_load(stream)

            requests_ = TestAlexFilm._requests
            if not requests_:
                alexfilm_config = config['secrets']['alexfilm']
                username = alexfilm_config['username']
                password = alexfilm_config['password']

                requests_ = requests.Session()
                requests_.auth = alexfilm.AlexFilmAuth(username, password)

                TestAlexFilm._requests = requests_

    def test_magnet(self):
        magnet = alexfilm.AlexFilm.get_marget(TestAlexFilm._requests, 1814)
        print(magnet)

        self.assertRaises(Exception)


if __name__ == '__main__':
    unittest.main()
