# -*- coding: utf-8 -*-

import json
import logging
from time import sleep
from typing import Text, Dict, List, Optional
from urllib.parse import urlparse

from flexget import plugin
from flexget.event import event
from flexget.plugin import PluginError
from flexget.utils.requests import Session
from requests import Session as RequestsSession

PLUGIN_NAME = 'flaresolverr'

CF_CLEARANCE_COOKIE_NAME = 'cf_clearance'
USER_AGENT = 'Mozilla/5.0 (X11; Linux x86_64; rv:94.0) Gecko/20100101 Firefox/94.0'

log = logging.getLogger(PLUGIN_NAME)


class FlareSolverrCookie(object):
    name: Text
    value: Text
    domain: Text
    path: Text
    expires: int
    size: int
    http_only: bool
    secure: bool
    session: bool
    same_site: Text

    def __init__(self, data: Dict):
        self.name = data.get('name')
        self.value = data.get('value')
        self.domain = data.get('domain')
        self.path = data.get('path')
        self.expires = data.get('expires')
        self.size = data.get('size')
        self.http_only = data.get('httpOnly')
        self.secure = data.get('secure')
        self.session = data.get('session')
        self.same_site = data.get('sameSite')


class FlareSolverrSolution(object):
    url: Text
    status: int
    headers: Dict[Text, Text]
    response: Text
    cookies: List[FlareSolverrCookie]
    user_agent: Text

    def __init__(self, data: Dict):
        self.url = data.get('url')
        self.status = data.get('status')
        self.headers = data.get('headers')
        self.response = data.get('response')

        cookies_data = data.get('cookies')
        self.cookies = [FlareSolverrCookie(x) for x in cookies_data] if cookies_data else []

        self.user_agent = data.get('userAgent')


class FlareSolverrResponse(object):
    status: Text
    message: Text
    start_timestamp: int
    end_timestamp: int
    version: Text

    solution: FlareSolverrSolution

    def __init__(self, data: Dict):
        self.status = data.get('status')
        self.message = data.get('message')
        self.start_timestamp = data.get('startTimestamp')
        self.end_timestamp = data.get('endTimestamp')
        self.version = data.get('version')

        solution_data = data.get('solution')
        self.solution = FlareSolverrSolution(solution_data) if solution_data else None


class ChallengeError(Exception):
    def __init__(self, status: Text, message: Text = None):
        self.status = status
        self.message = message
        super().__init__(self.status, self.message)


class FlareSolverr(Session):
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
        payload = {'cmd': 'request.get', 'url': challenge_url, 'returnOnlyCookies': True}

        log.info('Challenge "{0}"...'.format(challenge_url))
        response = self.perform_request('POST', self.__endpoint, json=payload, timeout=80)
        response.raise_for_status()

        challenge_response = FlareSolverrResponse(response.json())
        log.info('Challenge "{0}" completed with status: {1} ({2})'.format(
            challenge_url, challenge_response.status, challenge_response.message))

        if challenge_response.status != 'ok':
            raise ChallengeError(challenge_response.status, challenge_response.message)

        user_agent = challenge_response.solution.user_agent
        if user_agent and len(user_agent) > 0:
            self.headers.update({'User-Agent': user_agent})

        has_cf_clearance = False
        challenge_cookies = challenge_response.solution.cookies
        for x in challenge_cookies:
            if CF_CLEARANCE_COOKIE_NAME.lower() == x.name.lower():
                has_cf_clearance = True
            self.cookies.set(x.name, x.value, domain=x.domain, path=x.path, secure=x.secure, expires=x.expires)

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
    def create_solverr(cls, endpoint: Text, session: RequestsSession = None, **kwargs):
        solverr = cls(endpoint, **kwargs)

        if session:
            for attr in ['auth', 'cert', 'cookies', 'headers', 'hooks', 'params', 'proxies', 'data']:
                val = getattr(session, attr, None)
                if val is not None:
                    setattr(solverr, attr, val)

        return solverr


class FlareSolverrPlugin(object):
    """
        Plugin that enables scraping of cloudflare protected sites.

        Example::
          flaresolverr: 'http://localhost:8191/v1'

          flaresolverr:
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
            raise PluginError('FlareSolverr endpoint are not configured.')

        task.requests = FlareSolverr.create_solverr(endpoint, session=task.requests)


@event('plugin.register')
def register_plugin():
    plugin.register(FlareSolverrPlugin, PLUGIN_NAME, api_ver=2)
