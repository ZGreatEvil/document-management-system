# -*- coding: utf-8 -*-
"""an in-memory stand-in for GraphClient: one document library, enough of graph for the module's flows"""
import base64
import copy
import os
import re
from urllib.parse import unquote

from odoo.addons.document_management_system.models.graph_client import DmsGraphError

DRIVE_ID = 'drive-1'


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
        self.perms = {}      # item id -> list of permissions
        self.invites = {}    # item id -> list of (email, roles)
        self.sessions = {}
        self.calls = []
        self.fail = {}       # (method, part of the path) -> DmsGraphError raised once
        self.counter = 0

    def reset_calls(self):
        self.calls = []
        self.fail = {}

    def snapshot(self):
        """the library as it is now; restore() goes back to it, like the test database is rolled back"""
        return copy.deepcopy((self.items, self.links, self.perms, self.invites, self.sessions))

    def restore(self, state):
        self.items, self.links, self.perms, self.invites, self.sessions = copy.deepcopy(state)

    # ------------------------------------------------------------------
    # helpers for tests
    # ------------------------------------------------------------------

    def add_file(self, parent_id, name, size=10, mimetype='application/pdf', drive=DRIVE_ID):
        item = {'id': self._new_id('file'), 'name': name, 'parent': parent_id, 'size': size, 'mimetype': mimetype,
                'drive': drive}
        self.items[item['id']] = item
        return item['id']

    def add_folder(self, parent_id, name):
        item = {'id': self._new_id('folder'), 'name': name, 'parent': parent_id, 'folder': True}
        self.items[item['id']] = item
        return item['id']

    def copy_link(self, item_id, scope='organization'):
        """what a user's "copy link" in sharepoint leaves on an item: a link permission and its url"""
        url = 'https://sharepoint.test/copied/%s/%s' % (item_id, scope)
        self.links[url] = item_id
        self.perms.setdefault(item_id, []).append({
            'id': 'copied-%s-%s' % (item_id, scope), 'roles': ['read'],
            'link': {'type': 'view', 'scope': scope, 'webUrl': url}})
        return url

    def path_of(self, item_id):
        parts = []
        item = self.items.get(item_id)
        while item:
            parts.append(item['name'])
            item = self.items.get(item['parent'])
        return '/'.join(reversed(parts))

    def emails_on_link(self, item_id):
        link = next(p for p in self.perms.get(item_id, []) if p['id'] == 'perm-%s' % item_id)
        return set(link.get('emails', ()))

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

    def _view(self, item):
        found = {'id': item['id'], 'name': item['name'], 'webUrl': 'https://sharepoint.test/view/%s' % item['id'],
                 'parentReference': {'id': item['parent'], 'driveId': item.get('drive', DRIVE_ID)}}
        if item.get('folder'):
            found['folder'] = {}
        else:
            found['file'] = {'mimeType': item.get('mimetype', 'application/octet-stream')}
            found['size'] = item.get('size', 0)
        return found

    def _store_file(self, parent_id, name, size, conflict):
        if self._child(parent_id, name):
            if conflict == 'fail':
                raise DmsGraphError(409, 'nameAlreadyExists')
            stem, ext = os.path.splitext(name)
            name = '%s 1%s' % (stem, ext)
        item_id = self.add_file(parent_id, name, size)
        return self._view(self.items[item_id])

    # ------------------------------------------------------------------
    # GraphClient interface
    # ------------------------------------------------------------------

    def get(self, path, expect=(200,)):
        self._record('GET', path)
        match = re.match(r'^/shares/([^/]+)/driveItem', path)
        if match:
            item_id = self.links.get(decode_share(match.group(1)))
            if item_id not in self.items:
                raise DmsGraphError(404, 'itemNotFound')
            return self._view(self.items[item_id])
        match = re.match(r'^/drives/[^/]+/items/([^/:?]+)/permissions$', path)
        if match:
            return {'value': copy.deepcopy(self.perms.get(match.group(1), []))}
        match = re.match(r'^/drives/[^/]+/items/([^/:?]+)(\?.*)?$', path)
        if match:
            if match.group(1) not in self.items:
                raise DmsGraphError(404, 'itemNotFound')
            return self._view(self.items[match.group(1)])
        item = self._child(*self._parent_and_name(path))
        if not item:
            raise DmsGraphError(404, 'itemNotFound')
        return self._view(item)

    def post(self, path, json=None, expect=(200, 201)):
        self._record('POST', path)
        match = re.match(r'^/drives/[^/]+/items/([^/:]+)/children$', path)
        if match:
            if self._child(match.group(1), json['name']):
                raise DmsGraphError(409, 'nameAlreadyExists')
            return self._view(self.items[self.add_folder(match.group(1), json['name'])])
        match = re.match(r'^/drives/[^/]+/items/([^/:]+)/createLink$', path)
        if match:
            item_id = match.group(1)
            existing = next((p for p in self.perms.get(item_id, []) if p['id'] == 'perm-%s' % item_id), None)
            if existing:
                return copy.deepcopy(existing)
            url = 'https://sharepoint.test/link/%s' % item_id
            self.links[url] = item_id
            perm = {'id': 'perm-%s' % item_id, 'roles': ['read'], 'application': True, 'emails': [],
                    'link': {'type': json['type'], 'scope': json['scope'], 'webUrl': url}}
            self.perms.setdefault(item_id, []).append(perm)
            return copy.deepcopy(perm)
        match = re.match(r'^/shares/([^/]+)/permission/grant$', path)
        if match:
            url = decode_share(match.group(1))
            item_id = self.links[url]
            perm = next(p for p in self.perms[item_id] if p['link'].get('webUrl') == url)
            perm['emails'] = sorted(set(perm['emails']) | {r['email'] for r in json['recipients']})
            # grant may hand back a different url; the module must keep this one
            perm['link']['webUrl'] = url + '?granted'
            self.links[perm['link']['webUrl']] = item_id
            return {'value': [copy.deepcopy(perm)]}
        match = re.match(r'^/drives/[^/]+/items/([^/:]+)/invite$', path)
        if match:
            self.invites.setdefault(match.group(1), []).append(
                (json['recipients'][0]['email'], json['roles'], json['requireSignIn'], json['sendInvitation']))
            return {'value': [{'id': 'invite-%s' % match.group(1), 'roles': json['roles']}]}
        if path.endswith(':/createUploadSession'):
            parent_id, name = self._parent_and_name(path)
            url = 'https://upload.sharepoint.test/%s' % self._new_id('session')
            self.sessions[url] = {'parent': parent_id, 'name': name, 'received': 0,
                                  'conflict': json['item']['@microsoft.graph.conflictBehavior']}
            return {'uploadUrl': url}
        raise AssertionError('unexpected POST %s' % path)

    def put(self, path, data, content_type='application/octet-stream', expect=(200, 201)):
        self._record('PUT', path)
        parent_id, name = self._parent_and_name(path)
        conflict = re.search(r'conflictBehavior=(\w+)', path).group(1)
        return self._store_file(parent_id, name, len(data), conflict)

    def patch(self, path, json=None, expect=(200,)):
        self._record('PATCH', path)
        item = self.items[re.match(r'^/drives/[^/]+/items/([^/:?]+)$', path).group(1)]
        parent_id = json['parentReference']['id']
        taken = self._child(parent_id, json['name'])
        if taken and taken is not item:
            raise DmsGraphError(409, 'nameAlreadyExists')
        item['parent'], item['name'] = parent_id, json['name']
        return self._view(item)

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
        match = re.match(r'^/drives/[^/]+/items/([^/]+)/permissions/([^/]+)$', path)
        if match:
            perms = self.perms.get(match.group(1), [])
            self.perms[match.group(1)] = [p for p in perms if p['id'] != match.group(2)]
            return
        self.items.pop(path.rsplit('/', 1)[1], None)
