# -*- coding: utf-8 -*-
"""an in-memory stand-in for GraphClient: one document library, enough of graph for the module's flows"""
import base64
import copy
import os
import re
from urllib.parse import unquote

from odoo.addons.document_management_system.models.graph_client import DmsGraphError


class FakeResponse(object):
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}

    def json(self):
        return self._body


def decode_share(share_id):
    data = share_id[2:]
    return base64.urlsafe_b64decode(data + '=' * (-len(data) % 4)).decode()


class FakeGraph(object):

    def __init__(self):
        self.items = {}
        self.links = {}      # share url -> item id
        self.grants = {}     # item id -> set of emails
        self.sessions = {}
        self.calls = []
        self.fail = {}       # (method, part of the path) -> DmsGraphError raised once
        self.counter = 0

    def reset_calls(self):
        self.calls = []
        self.fail = {}

    def snapshot(self):
        """the library as it is now; restore() goes back to it, like the test database is rolled back"""
        return copy.deepcopy((self.items, self.links, self.grants, self.sessions))

    def restore(self, state):
        self.items, self.links, self.grants, self.sessions = copy.deepcopy(state)

    # ------------------------------------------------------------------

    def _new_id(self, prefix):
        self.counter += 1
        return '%s-%d' % (prefix, self.counter)

    def _record(self, method, path):
        self.calls.append((method, path))
        for (fail_method, part), error in list(self.fail.items()):
            if fail_method == method and part in path:
                del self.fail[(fail_method, part)]
                raise error

    def _child(self, parent_id, name):
        return next((i for i in self.items.values() if i['parent'] == parent_id and i['name'] == name), None)

    def _parent_and_name(self, path):
        match = re.match(r'^/drives/[^/]+/items/([^/:]+):/([^:?]+)', path)
        return match.group(1), unquote(match.group(2))

    def _store_file(self, parent_id, name, size, conflict):
        if self._child(parent_id, name):
            if conflict == 'fail':
                raise DmsGraphError(409, 'nameAlreadyExists')
            stem, ext = os.path.splitext(name)
            name = '%s 1%s' % (stem, ext)
        item = {'id': self._new_id('file'), 'name': name, 'parent': parent_id, 'size': size}
        self.items[item['id']] = item
        return {'id': item['id'], 'name': name, 'size': size, 'file': {}}

    # ------------------------------------------------------------------
    # GraphClient interface
    # ------------------------------------------------------------------

    def get(self, path, expect=(200,)):
        self._record('GET', path)
        item = self._child(*self._parent_and_name(path))
        if not item:
            raise DmsGraphError(404, 'itemNotFound')
        found = {'id': item['id'], 'name': item['name']}
        if item.get('folder'):
            found['folder'] = {}
        return found

    def post(self, path, json=None, expect=(200, 201)):
        self._record('POST', path)
        match = re.match(r'^/drives/[^/]+/items/([^/:]+)/children$', path)
        if match:
            if self._child(match.group(1), json['name']):
                raise DmsGraphError(409, 'nameAlreadyExists')
            item = {'id': self._new_id('folder'), 'name': json['name'], 'parent': match.group(1), 'folder': True}
            self.items[item['id']] = item
            return {'id': item['id'], 'name': item['name'], 'folder': {}}
        match = re.match(r'^/drives/[^/]+/items/([^/:]+)/createLink$', path)
        if match:
            url = 'https://sharepoint.test/link/%s' % match.group(1)
            self.links[url] = match.group(1)
            return {'id': 'perm-%s' % match.group(1), 'roles': ['read'],
                    'link': {'type': json['type'], 'scope': json['scope'], 'webUrl': url}}
        match = re.match(r'^/shares/([^/]+)/permission/grant$', path)
        if match:
            url = decode_share(match.group(1))
            item_id = self.links[url]
            self.grants.setdefault(item_id, set()).update(r['email'] for r in json['recipients'])
            # grant may hand back a different url; the module must keep this one
            new_url = url + '?granted'
            self.links[new_url] = item_id
            return {'value': [{'id': 'perm-%s' % item_id, 'roles': ['read'],
                               'link': {'type': 'view', 'scope': 'users', 'webUrl': new_url}}]}
        if path.endswith(':/createUploadSession'):
            parent_id, name = self._parent_and_name(path)
            url = 'https://upload.sharepoint.test/session-%d' % len(self.sessions)
            self.sessions[url] = {'parent': parent_id, 'name': name, 'received': 0,
                                  'conflict': json['item']['@microsoft.graph.conflictBehavior']}
            return {'uploadUrl': url}
        raise AssertionError('unexpected POST %s' % path)

    def put(self, path, data, content_type='application/octet-stream', expect=(200, 201)):
        self._record('PUT', path)
        parent_id, name = self._parent_and_name(path)
        conflict = re.search(r'conflictBehavior=(\w+)', path).group(1)
        return self._store_file(parent_id, name, len(data), conflict)

    def upload_chunk(self, upload_url, chunk, start, total):
        self._record('PUT', upload_url)
        session = self.sessions[upload_url]
        assert start == session['received'], 'chunks out of order'
        session['received'] += len(chunk)
        if session['received'] < total:
            return FakeResponse(202, {'nextExpectedRanges': ['%d-' % session['received']]})
        return FakeResponse(201, self._store_file(session['parent'], session['name'], total, session['conflict']))

    def request(self, method, path, expect=(200,), auth=True, **kwargs):
        self._record(method, path)
        if method == 'DELETE' and path in self.sessions:
            del self.sessions[path]
            return FakeResponse(204)
        raise AssertionError('unexpected %s %s' % (method, path))

    def delete(self, path, expect=(204,)):
        self._record('DELETE', path)
        item_id = path.rsplit('/', 1)[1]
        self.items.pop(item_id, None)

    # ------------------------------------------------------------------
    # for assertions
    # ------------------------------------------------------------------

    def path_of(self, item_id):
        parts = []
        item = self.items.get(item_id)
        while item:
            parts.append(item['name'])
            item = self.items.get(item['parent'])
        return '/'.join(reversed(parts))
