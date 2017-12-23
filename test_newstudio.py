import unittest
import yaml

import newstudio


class TestNewStudio(unittest.TestCase):
    def setUp(self):
        with open("test_config.yml", 'r') as stream:
            config = yaml.load(stream)
            self._username = config['secrets']['newstudio']['username']
            self._password = config['secrets']['newstudio']['password']

    def test_auth(self):
        auth_handler = newstudio.NewStudioAuth(self._username, self._password)
        print(auth_handler.cookies_)

        self.assertRaises(Exception)


if __name__ == '__main__':
    unittest.main()
