# -*- coding: utf-8 -*-

import unittest

import yaml

from . import flaresolverr


class TestFlareSolverr(unittest.TestCase):
    def setUp(self):
        with open("test_config.yml", 'r') as stream:
            config = yaml.safe_load(stream)
            endpoint = config['flaresolverr']

            self._requests = flaresolverr.FlareSolverr.create_solverr(endpoint)

    def tearDown(self):
        self._requests.close()
        del self._requests

    def test_challenge(self):
        response = self._requests.get('https://nowsecure.nl')
        response.raise_for_status()

        cookies = self._requests.cookies.get_dict(domain='.nowsecure.nl')
        for k, v in cookies.items():
            print('{0}: {1}'.format(k, v))


if __name__ == '__main__':
    unittest.main()
