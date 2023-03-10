# -*- coding: utf-8 -*-

import logging
from time import sleep
from typing import Text, Optional
from urllib.parse import urlparse

from flexget import plugin
from flexget.event import event
from flexget.plugin import PluginError
from flexget.utils.requests import Session
from requests import Session as RequestsSession

PLUGIN_NAME = 'clearance'

CF_CLEARANCE_COOKIE_NAME = 'cf_clearance'
USER_AGENT = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/111.0.0.0 Safari/537.36'

log = logging.getLogger(PLUGIN_NAME)


class ChallengeError(Exception):
    def __init__(self, message: Text = None):
        self.message = message
        super().__init__(self.message)


class Clearance(Session):
    def __init__(self, endpoint: Text, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.__endpoint = endpoint
        self.__whitelist = list()

        self.headers.update({'User-Agent': USER_AGENT})

    def perform_request(self, method, url, *args, **kwargs):
        return super().request(method, url, *args, **kwargs)

    def challenge(self, url: Text) -> None:
        parsed_url = urlparse(url)

        cookie_domain = parsed_url.netloc
        cookie_domain = '.{0}'.format('.'.join(cookie_domain.split('.')[-2:]))
        if cookie_domain.lower() in self.__whitelist:
            log.debug('"{0}" domain is whitelisted - skip challenge.'.format(cookie_domain))
            return

        domain_cookies = self.cookies.get_dict(domain=cookie_domain)
        if domain_cookies and len(domain_cookies) > 0 and CF_CLEARANCE_COOKIE_NAME in domain_cookies:
            log.debug(
                '"{0}" cookie exists for domain "{1}" - skip challenge.'.format(
                    CF_CLEARANCE_COOKIE_NAME, cookie_domain))
            return

        challenge_url = '{0}://{1}'.format(parsed_url.scheme, parsed_url.netloc)
        payload = {'url': challenge_url, 'timeout': 60}

        log.info('Challenge "{0}"...'.format(challenge_url))
        response = self.perform_request('POST', self.__endpoint, json=payload, timeout=80)
        response.raise_for_status()

        content = response.json()
        success = content['success']
        msg = content['msg']

        log.info('Challenge "{0}" completed with status: {1} ({2})'.format(challenge_url, success, msg))

        if not success:
            raise ChallengeError(msg)

        user_agent = content['user_agent']
        if user_agent and len(user_agent) > 0:
            self.headers.update({'User-Agent': user_agent})

        has_cf_clearance = False
        challenge_cookies = content['cookies']
        for k, v in challenge_cookies.items():
            if CF_CLEARANCE_COOKIE_NAME.lower() == k.lower():
                has_cf_clearance = True
            self.cookies.set(k, v, domain=cookie_domain)

        if not has_cf_clearance:
            log.info(
                '"{0}" cookie not found - add domain "{1}" to whitelist.'.format(
                    CF_CLEARANCE_COOKIE_NAME, cookie_domain))
            self.__whitelist.append(cookie_domain.lower())

    def try_challenge(self, url: Text) -> None:
        # try 3 times to pass challenge
        error = None
        for attempt in range(3):
            if attempt > 0:
                sleep(3)

            try:
                self.challenge(url)
                return
            except Exception as e:
                error = e
                log.warning(error)

        raise error

    def request(self, method, url, *args, **kwargs):
        self.try_challenge(url)
        return self.perform_request(method, url, *args, **kwargs)

    @classmethod
    def create_clearance(cls, endpoint: Text, session: RequestsSession = None, **kwargs):
        clearance = cls(endpoint, **kwargs)

        if session:
            for attr in ['auth', 'cert', 'cookies', 'headers', 'hooks', 'params', 'proxies', 'data']:
                val = getattr(session, attr, None)
                if val is not None:
                    setattr(clearance, attr, val)

        return clearance


class ClearancePlugin(object):
    """
        Plugin that enables scraping of cloudflare protected sites.

        Example::
          cf_clearance: 'http://localhost:8191/v1'

          cf_clearance:
            endpoint: 'http://localhost:8191/v1'
        """

    schema = {
        'oneOf': [
            {'type': 'string'},
            {
                'type': 'object',
                'properties': {
                    'endpoint': {'type': 'string'}
                },
                'additionalProperties': False
            }
        ]
    }

    @plugin.priority(253)
    def on_task_start(self, task, config):
        endpoint: Optional[str]

        if isinstance(config, str):
            endpoint = config
        else:
            endpoint = config.get('endpoint')

        if not endpoint or len(endpoint) <= 0:
            raise PluginError('Clearance endpoint are not configured.')

        task.requests = Clearance.create_clearance(endpoint, session=task.requests)


@event('plugin.register')
def register_plugin():
    plugin.register(ClearancePlugin, PLUGIN_NAME, api_ver=2)
