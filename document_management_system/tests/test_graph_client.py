# -*- coding: utf-8 -*-
import base64
import json
from unittest.mock import patch

import requests

from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo.tools import config

from odoo.addons.document_management_system.models import graph_client as gc

LOGGER = 'odoo.addons.document_management_system.models.graph_client'


class FakeResp(object):
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.content = b'' if body is None else json.dumps(body).encode()

    def json(self):
        if self._body is None:
            raise ValueError('no body')
        return self._body


def graph_error(status, code, inner=None, headers=None):
    err = {'code': code, 'message': 'details'}
    if inner:
        err['innerError'] = {'code': inner}
    return FakeResp(status, {'error': err}, headers)


class FakeSession(object):
    """stands in for requests.Session: replies are handed out in order, tokens are tok1, tok2, ..."""

    def __init__(self, replies=(), token_replies=()):
        self.replies = list(replies)
        self.token_replies = list(token_replies)
        self.calls = []
        self.token_calls = 0

    def post(self, url, timeout=None, data=None):
        self.token_calls += 1
        if self.token_replies:
            return self.token_replies.pop(0)
        return FakeResp(200, {'access_token': 'tok%d' % self.token_calls, 'expires_in': 3599})

    def request(self, method, url, headers=None, json=None, data=None, timeout=None):
        self.calls.append({'method': method, 'url': url, 'headers': headers or {}, 'json': json, 'data': data})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


@tagged('post_install', '-at_install', 'dms')
class TestGraphClient(TransactionCase):

    def setUp(self):
        super().setUp()
        gc._token_cache.clear()
        self.addCleanup(gc._token_cache.clear)
        self.sleeps = []

    def client(self, replies=(), token_replies=()):
        session = FakeSession(replies, token_replies)
        return gc.GraphClient('tenant', 'app', 'very-secret', session=session, sleep=self.sleeps.append), session

    def test_encode_share_url(self):
        # the steps from microsoft's docs: base64, strip '=', '/' -> '_', '+' -> '-', prefix u!
        url = 'https://onedrive.live.com/redir?resid=1231244193912!12&authKey=1201919!12921!1'
        manual = base64.b64encode(url.encode()).decode().rstrip('=').replace('/', '_').replace('+', '-')
        self.assertEqual(gc.encode_share_url(url), 'u!' + manual)
        self.assertNotIn('=', gc.encode_share_url(url + '?'))

    def test_token_cached_between_calls(self):
        client, session = self.client([FakeResp(200, {'id': 'a'}), FakeResp(200, {'id': 'b'})])
        self.assertEqual(client.get('/sites/x')['id'], 'a')
        self.assertEqual(client.get('/sites/y')['id'], 'b')
        self.assertEqual(session.token_calls, 1)
        self.assertEqual([c['headers']['Authorization'] for c in session.calls], ['Bearer tok1', 'Bearer tok1'])
        self.assertTrue(session.calls[0]['url'].startswith(gc.GRAPH_URL + '/sites/x'))

    def test_error_carries_graph_code(self):
        client, _session = self.client([graph_error(404, 'itemNotFound')])
        with self.assertRaises(gc.DmsGraphError) as caught:
            client.get('/drives/d/items/i')
        self.assertEqual((caught.exception.status, caught.exception.code), (404, 'itemNotFound'))

    def test_inner_error_code_kept(self):
        client, _session = self.client([graph_error(403, 'accessDenied', inner='notAllowed')])
        with self.assertRaises(gc.DmsGraphError) as caught:
            client.post('/drives/d/items/i/createLink', json={})
        self.assertEqual(caught.exception.code, 'accessDenied/notAllowed')

    def test_retry_after_honoured(self):
        client, session = self.client([graph_error(429, 'TooManyRequests', headers={'Retry-After': '7'}),
                                       FakeResp(200, {'ok': True})])
        self.assertTrue(client.get('/sites/x')['ok'])
        self.assertEqual(self.sleeps, [7])
        self.assertEqual(len(session.calls), 2)

    def test_retry_after_capped(self):
        client, _session = self.client([graph_error(503, 'serviceNotAvailable', headers={'Retry-After': '3600'}),
                                        FakeResp(200, {})])
        client.get('/sites/x')
        self.assertEqual(self.sleeps, [gc.MAX_WAIT])

    def test_server_errors_back_off_then_succeed(self):
        client, _session = self.client([graph_error(503, 'serviceNotAvailable'), graph_error(502, 'badGateway'),
                                        FakeResp(201, {'id': 'new'})])
        self.assertEqual(client.post('/x', json={})['id'], 'new')
        self.assertEqual(self.sleeps, [1, 2])

    def test_gives_up_after_max_tries(self):
        client, session = self.client([graph_error(503, 'serviceNotAvailable')] * gc.MAX_TRIES)
        with self.assertRaises(gc.DmsGraphError) as caught:
            client.get('/sites/x')
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(len(session.calls), gc.MAX_TRIES)
        self.assertEqual(len(self.sleeps), gc.MAX_TRIES - 1)

    def test_client_errors_not_retried(self):
        client, session = self.client([graph_error(409, 'nameAlreadyExists')])
        with self.assertRaises(gc.DmsGraphError):
            client.post('/drives/d/items/p/children', json={})
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(self.sleeps, [])

    def test_network_error_retried(self):
        client, _session = self.client([requests.ConnectionError('down'), FakeResp(200, {'id': 'a'})])
        self.assertEqual(client.get('/sites/x')['id'], 'a')
        self.assertEqual(self.sleeps, [1])

    def test_network_error_gives_up(self):
        client, _session = self.client([requests.ConnectionError('down')] * gc.MAX_TRIES)
        with self.assertRaises(gc.DmsGraphError) as caught:
            client.get('/sites/x')
        self.assertEqual((caught.exception.status, caught.exception.code), (0, 'networkError'))

    def test_expired_token_renewed_once(self):
        client, session = self.client([graph_error(401, 'InvalidAuthenticationToken'), FakeResp(200, {'id': 'a'})])
        self.assertEqual(client.get('/sites/x')['id'], 'a')
        self.assertEqual(session.token_calls, 2)
        self.assertEqual(session.calls[1]['headers']['Authorization'], 'Bearer tok2')

    def test_second_401_raises(self):
        client, _session = self.client([graph_error(401, 'InvalidAuthenticationToken')] * 2)
        with self.assertRaises(gc.DmsGraphError) as caught:
            client.get('/sites/x')
        self.assertEqual(caught.exception.status, 401)

    def test_token_refused(self):
        client, _session = self.client(token_replies=[FakeResp(401, {'error': 'invalid_client',
                                                                      'error_description': 'bad secret'})])
        with self.assertRaises(gc.DmsGraphError) as caught:
            client.get('/sites/x')
        self.assertEqual((caught.exception.status, caught.exception.code), (401, 'invalid_client'))

    def test_upload_chunk_has_no_authorization(self):
        upload_url = 'https://tenant.sharepoint.com/_api/v2.0/uploadSession?guid=1&tempauth=abc'
        client, session = self.client([FakeResp(202, {'nextExpectedRanges': ['10-']})])
        client.upload_chunk(upload_url, b'0123456789', 0, 100)
        sent = session.calls[0]
        self.assertNotIn('Authorization', sent['headers'])
        self.assertEqual(sent['headers']['Content-Range'], 'bytes 0-9/100')
        self.assertEqual(sent['url'], upload_url)
        self.assertEqual(session.token_calls, 0)

    def test_delete_and_empty_bodies(self):
        client, _session = self.client([FakeResp(204)])
        self.assertIsNone(client.delete('/drives/d/items/i/permissions/p'))

    def test_logs_hold_no_secrets(self):
        upload_url = 'https://tenant.sharepoint.com/upload?tempauth=SECRETAUTH'
        share = gc.encode_share_url('https://tenant.sharepoint.com/:w:/s/site/SECRETLINK')
        client, _session = self.client([
            graph_error(429, 'TooManyRequests', headers={'Retry-After': '0'}), graph_error(403, 'accessDenied'),
            graph_error(404, 'itemNotFound'), graph_error(400, 'invalidRequest'),
        ])
        with self.assertLogs(LOGGER, level='INFO') as logs:
            with self.assertRaises(gc.DmsGraphError):
                client.get('/shares/%s/driveItem' % share)
            with self.assertRaises(gc.DmsGraphError):
                client.get('/sites/x')
            with self.assertRaises(gc.DmsGraphError):
                client.upload_chunk(upload_url, b'x', 0, 1)
        text = '\n'.join(logs.output)
        for secret in ('tok1', 'very-secret', 'SECRETAUTH', share, 'details'):
            self.assertNotIn(secret, text)
        self.assertIn('/shares/<link>/driveItem', text)
        self.assertIn('<uploadUrl>', text)
        self.assertIn('accessDenied', text)

    def test_not_configured(self):
        with self.assertRaises(gc.DmsGraphError) as caught:
            gc.GraphClient('tenant', 'app', '')
        self.assertEqual(caught.exception.code, 'notConfigured')

    def test_from_env_reads_settings_and_conf(self):
        params = self.env['ir.config_parameter'].sudo()
        params.set_param(gc.PARAM_PREFIX + 'tenant_id', 'tenant-from-settings')
        params.set_param(gc.PARAM_PREFIX + 'client_id', 'app-from-settings')
        with patch.dict(config.options, {gc.SECRET_KEY: 'secret-from-conf'}):
            client = gc.GraphClient.from_env(self.env)
            self.assertEqual((client.tenant_id, client.client_id), ('tenant-from-settings', 'app-from-settings'))
            self.assertEqual(client._secret, 'secret-from-conf')
            self.assertEqual(gc.GraphClient.from_env(self.env, tenant_id='other').tenant_id, 'other')
        with patch.dict(config.options, {gc.SECRET_KEY: ''}):
            with self.assertRaises(gc.DmsGraphError):
                gc.GraphClient.from_env(self.env)


@tagged('post_install', '-at_install', 'dms')
class TestSettings(TransactionCase):

    def settings(self, **values):
        vals = {'dms_tenant_id': 'tenant', 'dms_client_id': 'app', 'dms_site_id': 'site'}
        vals.update(values)
        return self.env['res.config.settings'].create(vals)

    def test_settings_saved_as_parameters(self):
        self.settings(dms_drive_id='drive-1', dms_projects_folder_id='p-1', dms_intake_folder_id='i-1').execute()
        saved = gc.get_settings(self.env)
        self.assertEqual(saved['drive_id'], 'drive-1')
        self.assertEqual(saved['projects_folder_id'], 'p-1')
        self.assertEqual(saved['intake_folder_id'], 'i-1')
        self.assertEqual(saved['site_id'], 'site')

    def test_secret_is_never_a_parameter(self):
        with patch.dict(config.options, {gc.SECRET_KEY: 'secret-from-conf'}):
            settings = self.settings()
            self.assertTrue(settings.dms_secret_set)
            settings.execute()
        stored = self.env['ir.config_parameter'].sudo().search([('value', 'ilike', 'secret-from-conf')])
        self.assertFalse(stored)

    def test_connection_ok(self):
        with patch.dict(config.options, {gc.SECRET_KEY: 'secret'}), \
                patch.object(gc.GraphClient, 'test_connection', return_value='Project Documents') as tested:
            action = self.settings().action_dms_test_connection()
        tested.assert_called_once_with('site')
        self.assertEqual(action['tag'], 'display_notification')
        self.assertIn('Project Documents', action['params']['message'])

    def test_connection_without_secret(self):
        with patch.dict(config.options, {gc.SECRET_KEY: ''}):
            with self.assertRaisesRegex(UserError, gc.SECRET_KEY):
                self.settings().action_dms_test_connection()

    def test_connection_missing_ids(self):
        with patch.dict(config.options, {gc.SECRET_KEY: 'secret'}):
            with self.assertRaises(UserError):
                self.settings(dms_site_id=False).action_dms_test_connection()

    def test_connection_refused(self):
        with patch.dict(config.options, {gc.SECRET_KEY: 'secret'}), \
                patch.object(gc.GraphClient, 'test_connection', side_effect=gc.DmsGraphError(403, 'accessDenied')):
            with self.assertRaisesRegex(UserError, 'accessDenied'):
                self.settings().action_dms_test_connection()
