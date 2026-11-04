# -*- coding: utf-8 -*-
"""small microsoft graph client. every graph call of the module goes through it; tests mock it.

the client secret lives in odoo.conf (dms_graph_client_secret), never in the database.
only status codes and graph error codes are logged, never tokens or response bodies.
"""
import base64
import logging
import threading
import time

import requests

from odoo.tools import config

_logger = logging.getLogger(__name__)

GRAPH_URL = 'https://graph.microsoft.com/v1.0'
TOKEN_URL = 'https://login.microsoftonline.com/%s/oauth2/v2.0/token'
SECRET_KEY = 'dms_graph_client_secret'
PARAM_PREFIX = 'document_management_system.'
SETTING_KEYS = ('tenant_id', 'client_id', 'site_id', 'drive_id', 'projects_folder_id', 'intake_folder_id')

RETRY_STATUSES = (429, 500, 502, 503, 504)
MAX_TRIES = 5
MAX_WAIT = 60
TIMEOUT = 60
# a token is renewed this many seconds before it expires
TOKEN_MARGIN = 300
# upload session chunks must be multiples of 320 KiB; 16 x 320 KiB = 5 MiB
UPLOAD_CHUNK = 16 * 327680

# one token per tenant and app, per worker process
_token_cache = {}
_token_lock = threading.Lock()


class DmsGraphError(Exception):
    """a graph call failed. `code` is graph's error code (e.g. itemNotFound), `status` the http status
    (0 when no answer came back)"""

    def __init__(self, status, code='', message=''):
        self.status = status
        self.code = code or ''
        self.graph_message = message or ''
        super().__init__(('Microsoft Graph error %s %s' % (status, self.code)).strip())


def encode_share_url(url):
    """a sharing url in the form /shares/{id} expects: unpadded base64url with a u! prefix"""
    return 'u!' + base64.urlsafe_b64encode(url.encode('utf-8')).decode('ascii').rstrip('=')


def get_settings(env):
    params = env['ir.config_parameter'].sudo()
    return {key: (params.get_param(PARAM_PREFIX + key) or '').strip() for key in SETTING_KEYS}


def _error(resp):
    try:
        err = resp.json().get('error') or {}
    except ValueError:
        return '', ''
    if isinstance(err, str):
        # the token endpoint answers {"error": "invalid_client", "error_description": ...}
        return err, ''
    code = err.get('code') or ''
    inner = err.get('innerError') or err.get('innererror') or {}
    if inner.get('code') and inner.get('code') != code:
        code = '%s/%s' % (code, inner['code'])
    return code, err.get('message') or ''


def _log_path(url):
    # upload urls are pre-authenticated and share ids contain the link: neither goes in the log
    if not url.startswith(GRAPH_URL):
        return '<uploadUrl>'
    path = url[len(GRAPH_URL):].split('?')[0]
    if path.startswith('/shares/'):
        _share, slash, rest = path[len('/shares/'):].partition('/')
        path = '/shares/<link>' + slash + rest
    return path


def _retry_wait(resp, attempt):
    try:
        wait = int(resp.headers.get('Retry-After'))
    except (TypeError, ValueError):
        wait = 2 ** attempt
    return max(0, min(wait, MAX_WAIT))


class GraphClient(object):

    def __init__(self, tenant_id, client_id, client_secret, session=None, sleep=time.sleep):
        if not (tenant_id and client_id and client_secret):
            raise DmsGraphError(0, 'notConfigured')
        self.tenant_id = tenant_id
        self.client_id = client_id
        self._secret = client_secret
        self.http = session or requests.Session()
        self._sleep = sleep

    @classmethod
    def from_env(cls, env, **overrides):
        """a client built from the settings screen and odoo.conf; overrides replace saved settings"""
        settings = get_settings(env)
        settings.update({key: value for key, value in overrides.items() if value is not None})
        return cls(settings['tenant_id'], settings['client_id'], config.get(SECRET_KEY))

    # ------------------------------------------------------------------
    # token
    # ------------------------------------------------------------------

    def _token(self, force=False):
        key = (self.tenant_id, self.client_id)
        with _token_lock:
            cached = _token_cache.get(key)
            if cached and not force and cached[1] - TOKEN_MARGIN > time.time():
                return cached[0]
        try:
            resp = self.http.post(TOKEN_URL % self.tenant_id, timeout=TIMEOUT, data={
                'client_id': self.client_id,
                'client_secret': self._secret,
                'scope': 'https://graph.microsoft.com/.default',
                'grant_type': 'client_credentials',
            })
        except requests.RequestException as e:
            _logger.warning('graph token request failed: %s', type(e).__name__)
            raise DmsGraphError(0, 'networkError')
        if resp.status_code != 200:
            code, _message = _error(resp)
            _logger.warning('graph token request failed: HTTP %s %s', resp.status_code, code)
            raise DmsGraphError(resp.status_code, code)
        data = resp.json()
        token = data['access_token']
        with _token_lock:
            _token_cache[key] = (token, time.time() + int(data.get('expires_in', 3599)))
        return token

    # ------------------------------------------------------------------
    # requests
    # ------------------------------------------------------------------

    def request(self, method, path, expect=(200,), auth=True, headers=None, json=None, data=None):
        """one graph call. retries 429 and 5xx (honouring Retry-After) and network errors,
        renews the token once on a 401, and raises DmsGraphError when the status isn't expected"""
        url = path if path.startswith('https://') else GRAPH_URL + path
        renewed = False
        resp = None
        for attempt in range(MAX_TRIES):
            sent = dict(headers or {})
            if auth:
                sent['Authorization'] = 'Bearer %s' % self._token(force=renewed)
            try:
                resp = self.http.request(method, url, headers=sent, json=json, data=data, timeout=TIMEOUT)
            except requests.RequestException as e:
                if attempt == MAX_TRIES - 1:
                    _logger.warning('graph %s %s failed: %s', method, _log_path(url), type(e).__name__)
                    raise DmsGraphError(0, 'networkError')
                self._sleep(2 ** attempt)
                continue
            if resp.status_code == 401 and auth and not renewed:
                renewed = True
                continue
            if resp.status_code in RETRY_STATUSES and attempt < MAX_TRIES - 1:
                wait = _retry_wait(resp, attempt)
                _logger.info('graph %s %s: HTTP %s, retrying in %ss', method, _log_path(url), resp.status_code, wait)
                self._sleep(wait)
                continue
            break
        if resp.status_code not in expect:
            code, message = _error(resp)
            _logger.warning('graph %s %s failed: HTTP %s %s', method, _log_path(url), resp.status_code, code)
            raise DmsGraphError(resp.status_code, code, message)
        return resp

    @staticmethod
    def _json(resp):
        return resp.json() if resp.content else {}

    def get(self, path, expect=(200,)):
        return self._json(self.request('GET', path, expect=expect))

    def post(self, path, json=None, expect=(200, 201)):
        return self._json(self.request('POST', path, expect=expect, json=json))

    def patch(self, path, json=None, expect=(200,)):
        return self._json(self.request('PATCH', path, expect=expect, json=json))

    def put(self, path, data, content_type='application/octet-stream', expect=(200, 201)):
        return self._json(self.request('PUT', path, expect=expect, data=data, headers={'Content-Type': content_type}))

    def delete(self, path, expect=(204,)):
        self.request('DELETE', path, expect=expect)

    def upload_chunk(self, upload_url, chunk, start, total):
        """one range of an upload session. no Authorization header: the url is pre-authenticated"""
        end = start + len(chunk) - 1
        return self.request('PUT', upload_url, expect=(200, 201, 202), auth=False, data=chunk,
                            headers={'Content-Range': 'bytes %d-%d/%d' % (start, end, total)})

    def test_connection(self, site_id):
        """gets a token and reads the site; returns its display name"""
        site = self.get('/sites/%s?$select=id,displayName' % site_id)
        return site.get('displayName') or site.get('id')
