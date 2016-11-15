# -*- coding: utf-8 -*-
from __future__ import unicode_literals, division, absolute_import
from builtins import *  # pylint: disable=unused-import, redefined-builtin

import json
import logging
from time import sleep
from datetime import datetime, timedelta

from sqlalchemy import Column, Unicode, Integer, DateTime
from sqlalchemy.types import TypeDecorator, VARCHAR

from requests.auth import AuthBase

from flexget import plugin
from flexget.event import event
from flexget.utils import requests
from flexget.manager import Session
from flexget.db_schema import versioned_base
from flexget.plugin import PluginError


plugin_name = 'alexfilm_auth'

Base = versioned_base(plugin_name, 0)
log = logging.getLogger(plugin_name)


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


class AlexFilmAccount(Base):
    __tablename__ = 'alexfilm_accounts'
    id = Column(Integer, primary_key=True, autoincrement=True, nullable=False)
    username = Column(Unicode, index=True, nullable=False, unique=True)
    cookies = Column(JSONEncodedDict)
    expiry_time = Column(DateTime, nullable=False)


class AlexFilmAuth(AuthBase):
    """Supports downloading of torrents from 'alexfilm' tracker
           if you pass cookies (CookieJar) to constructor then authentication will be bypassed and cookies will be just set
        """

    def try_authenticate(self, payload):
        for _ in range(5):
            session = requests.Session()
            session.post('http://alexfilm.cc/login.php', data=payload)
            cookies = session.cookies.get_dict(domain='.alexfilm.cc')
            if cookies and len(cookies) > 0:
                return cookies
            sleep(3)
        raise PluginError('Unable to obtain cookies from AlexFilm. Looks like invalid username or password.')

    def __init__(self, username, password, cookies=None, db_session=None):
        if cookies is None:
            log.debug('AlexFilm cookie not found. Requesting new one.')

            payload_ = {
                'login_username': username,
                'login_password': password,
                'login': "Вход",
                'autologin': 1
            }

            self.cookies_ = self.try_authenticate(payload_)
            if db_session:
                db_session.add(
                    AlexFilmAccount(
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


class PluginAlexFilmAuth(object):
    """Usage:

    alexfilm_auth:
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
        account = db_session.query(AlexFilmAccount).filter(AlexFilmAccount.username == username).first()
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
            auth_handler = AlexFilmAuth(username, password, cookies, db_session)
            self.auth_cache[username] = auth_handler
        else:
            auth_handler = self.auth_cache[username]

        return auth_handler

    @plugin.priority(127)
    def on_task_start(self, task, config):
        auth_handler = self.get_auth_handler(config)
        task.requests.auth = auth_handler


@event('plugin.register')
def register_plugin():
    plugin.register(PluginAlexFilmAuth, plugin_name, api_ver=2)
