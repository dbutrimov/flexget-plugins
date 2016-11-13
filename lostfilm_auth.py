# -*- coding: utf-8 -*-
from __future__ import unicode_literals, division, absolute_import
from builtins import *  # pylint: disable=unused-import, redefined-builtin

import json
import logging
import re
from time import sleep
from datetime import datetime, timedelta

from bs4 import BeautifulSoup

from sqlalchemy import Column, Unicode, Integer, DateTime
from sqlalchemy.types import TypeDecorator, VARCHAR

# import requests
from requests.auth import AuthBase

from flexget import plugin
from flexget.event import event
from flexget.utils import requests
from flexget.manager import Session
from flexget.db_schema import versioned_base
from flexget.plugin import PluginError


Base = versioned_base('lostfilm_auth', 0)
log = logging.getLogger('lostfilm_auth')

url_regexp = re.compile(r'^https?://(?:www\.)?lostfilm\.tv.*$', flags=re.IGNORECASE)


class JSONEncodedDict(TypeDecorator):
    """Represents an immutable structure as a json-encoded string.

    Usage:

        JSONEncodedDict(255)

    """

    impl = VARCHAR

    def process_bind_param(self, value, dialect):
        if value is not None:
            value = json.dumps(value)

        return value

    def process_result_value(self, value, dialect):
        if value is not None:
            value = json.loads(value)
        return value


class LostFilmAccount(Base):
    __tablename__ = 'lostfilm_accounts'
    id = Column(Integer, primary_key=True, autoincrement=True, nullable=False)
    username = Column(Unicode, index=True, nullable=False, unique=True)
    cookies = Column(JSONEncodedDict)
    expiry_time = Column(DateTime, nullable=False)


class LostFilmAuth(AuthBase):
    """Supports downloading of torrents from 'lostfilm' tracker
           if you pass cookies (CookieJar) to constructor then authentication will be bypassed and cookies will be just set
        """

    def try_authenticate(self, payload):
        for _ in range(5):
            session = requests.Session()
            session.headers['User-Agent'] = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_12_1) ' \
                                            'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/54.0.2840.98 Safari/537.36'

            response = session.post(
                'http://login1.bogi.ru/login.php?referer=http%3A%2F%2Fwww.lostfilm.tv%2F',
                data=payload)
            response_html = response.text

            response_tree = BeautifulSoup(response_html, 'html.parser')
            form_node = response_tree.find('form', id='b_form')
            if form_node:
                action_url = form_node.get('action')

                action_payload = {}
                input_nodes = form_node.find_all('input', type='hidden')
                for input_node in input_nodes:
                    input_name = input_node.get('name')
                    input_value = input_node.get('value')
                    action_payload[input_name] = input_value

                session.post(action_url, data=action_payload)

                cookies = session.cookies.get_dict(domain='.lostfilm.tv')
                if cookies and len(cookies) > 0:
                    user_id = cookies.get('uid')
                    user_pass = cookies.get('pass')
                    if user_id and user_pass:
                        cookies = {'uid': user_id, 'pass': user_pass}
                        return cookies

            sleep(3)

        raise PluginError('Unable to obtain cookies from LostFilm. Looks like invalid username or password.')

    def __init__(self, username, password, cookies=None, db_session=None):
        if cookies is None:
            log.debug('LostFilm cookie not found. Requesting new one.')

            payload_ = {
                'login': username,
                'password': password,
                'module': 1,
                'target': 'http://lostfilm.tv/',
                'repage': 'user',
                'act': 'login'
            }

            self.cookies_ = self.try_authenticate(payload_)
            if db_session:
                db_session.add(
                    LostFilmAccount(
                        username=username,
                        cookies=self.cookies_,
                        expiry_time=datetime.now() + timedelta(days=1)))
                db_session.commit()
            # else:
            #     raise ValueError(
            #         'db_session can not be None if cookies is None')
        else:
            log.debug('Using previously saved cookie.')
            self.cookies_ = cookies

    def __call__(self, r):
        r.prepare_cookies(self.cookies_)
        return r


class LostFilmUrlrewrite(object):
    """Usage:

    lostfilm_auth:
      username: 'username_here'
      password: 'password_here'
    """

    schema = {
        'type': 'object',
        'properties': {
            'username': {'type': 'string'},
            'password': {'type': 'string'}
        },
        "additionalProperties": False
    }

    auth_cache = {}

    def try_find_cookie(self, db_session, username):
        account = db_session.query(LostFilmAccount).filter(LostFilmAccount.username == username).first()
        if account:
            if account.expiry_time < datetime.now():
                db_session.delete(account)
                db_session.commit()
                return None
            return account.cookies
        else:
            return None

    def get_auth_handler(self, config):
        username = config.get('username')
        if not username or len(username) <= 0:
            raise PluginError('Username are not configured.')
        password = config.get('password')
        if not password or len(password) <= 0:
            raise PluginError('Password are not configured.')

        db_session = Session()
        cookies = self.try_find_cookie(db_session, username)
        if username not in self.auth_cache:
            auth_handler = LostFilmAuth(username, password, cookies, db_session)
            self.auth_cache[username] = auth_handler
        else:
            auth_handler = self.auth_cache[username]

        return auth_handler

    @plugin.priority(127)
    def on_task_urlrewrite(self, task, config):
        auth_handler = self.get_auth_handler(config)
        for entry in task.accepted:
            url = entry['url']
            if url_regexp.match(url):
                entry['download_auth'] = auth_handler


@event('plugin.register')
def register_plugin():
    plugin.register(LostFilmUrlrewrite, 'lostfilm_auth', api_ver=2)
