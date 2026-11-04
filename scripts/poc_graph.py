# -*- coding: utf-8 -*-
"""phase 0 proof of concept for document_management_system.

runs every microsoft graph call the module needs against a TEST site, with an
app that only has Sites.Selected + fullcontrol on that site, and reports which
calls work. python 3.7, needs only `requests`.

settings come from environment variables only:
  DMS_TENANT_ID, DMS_CLIENT_ID, DMS_CLIENT_SECRET, DMS_SITE_ID   required
  DMS_TEST_MEMBER_EMAIL   user who gets the file link (required)
  DMS_TEST_INTAKE_EMAIL   user who gets the intake folder (default: the member)
  DMS_DRIVE_ID            document library id (default: the site's default library)
  DMS_TEST_PASTED_LINK    optional: a link made with "copy link" in sharepoint, resolved through /shares

usage:
  python scripts/poc_graph.py                      run every step inside a new PoC-<timestamp> folder
  python scripts/poc_graph.py --cleanup PoC-...    delete that test folder afterwards

only status codes and graph error codes are printed, never tokens, secrets or response bodies.
"""
import argparse
import base64
import datetime
import json
import os
import sys
import time
from urllib.parse import quote

import requests

GRAPH = 'https://graph.microsoft.com/v1.0'
TOKEN_URL = 'https://login.microsoftonline.com/%s/oauth2/v2.0/token'
# upload session chunks must be multiples of 320 KiB; 16 x 320 KiB = 5 MiB
CHUNK = 16 * 327680
# not a multiple of the chunk size, so the last chunk is a short one
LARGE_SIZE = 12 * 1024 * 1024 + 12345
RETRY_STATUSES = (429, 500, 502, 503, 504)
MAX_TRIES = 5

SMALL_NAME = 'poc-small.txt'
LARGE_NAME = 'poc-large.bin'
CANCEL_NAME = 'poc-cancelled.bin'
INTAKE_NAME = 'intake sample.txt'
MOVED_NAME = 'QTN-001_TEST001_XI_26.txt'
CODE_FOLDER = 'TEST001'


class PocError(Exception):
    def __init__(self, status, code, where):
        self.status, self.code, self.where = status, code, where
        super().__init__('%s -> HTTP %s %s' % (where, status, code))


class CheckFailed(Exception):
    pass


def check(condition, message):
    if not condition:
        raise CheckFailed(message)


def error_code(resp):
    try:
        err = resp.json().get('error') or {}
    except ValueError:
        return ''
    if isinstance(err, str):
        # the token endpoint returns {"error": "invalid_client", ...}
        return err
    code = err.get('code') or ''
    inner = err.get('innerError') or err.get('innererror') or {}
    if inner.get('code') and inner.get('code') != code:
        code = '%s/%s' % (code, inner['code'])
    return code


def short(url):
    # upload urls are pre-authenticated: never print them
    if url.startswith('https://') and not url.startswith(GRAPH):
        return '<uploadUrl>'
    path = url.replace(GRAPH, '').split('?')[0]
    return path if len(path) <= 90 else path[:87] + '...'


def retry_wait(resp, attempt):
    try:
        return min(int(resp.headers.get('Retry-After')), 60)
    except (TypeError, ValueError):
        return 2 ** attempt


def encode_share(url):
    return 'u!' + base64.urlsafe_b64encode(url.encode('utf-8')).decode('ascii').rstrip('=')


def child_path(drive_id, parent_id, name):
    return '/drives/%s/items/%s:/%s' % (drive_id, parent_id, quote(name))


class Graph(object):
    def __init__(self, tenant, client_id, secret):
        self.tenant, self.client_id, self.secret = tenant, client_id, secret
        self.http = requests.Session()
        self._token = None
        self._expires = 0

    def token(self):
        if self._token and time.time() < self._expires - 300:
            return self._token
        resp = self.http.post(TOKEN_URL % self.tenant, timeout=30, data={
            'client_id': self.client_id,
            'client_secret': self.secret,
            'scope': 'https://graph.microsoft.com/.default',
            'grant_type': 'client_credentials',
        })
        if resp.status_code != 200:
            raise PocError(resp.status_code, error_code(resp), 'token')
        data = resp.json()
        self._token = data['access_token']
        self._expires = time.time() + int(data.get('expires_in', 3599))
        return self._token

    def token_roles(self):
        """the app permissions in the token (its `roles` claim), for the report only"""
        payload = self.token().split('.')[1]
        payload += '=' * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode('ascii'))).get('roles') or []

    def call(self, method, url, expect=(200,), auth=True, headers=None, **kwargs):
        if not url.startswith('https://'):
            url = GRAPH + url
        resp = None
        for attempt in range(MAX_TRIES):
            sent = dict(headers or {})
            if auth:
                sent['Authorization'] = 'Bearer ' + self.token()
            resp = self.http.request(method, url, headers=sent, timeout=120, **kwargs)
            if resp.status_code in RETRY_STATUSES and attempt < MAX_TRIES - 1:
                wait = retry_wait(resp, attempt)
                print('      %s %s -> HTTP %s, retrying in %ss' % (method, short(url), resp.status_code, wait))
                time.sleep(wait)
                continue
            break
        if resp.status_code not in expect:
            raise PocError(resp.status_code, error_code(resp), '%s %s' % (method, short(url)))
        return resp


def link_emails(perm):
    """lower-case emails of the people on a link permission"""
    emails = set()
    for key in ('grantedToIdentitiesV2', 'grantedToIdentities'):
        for identity in perm.get(key) or []:
            for part in ('user', 'siteUser'):
                who = identity.get(part) or {}
                if who.get('email'):
                    emails.add(who['email'].lower())
                login = who.get('loginName') or ''
                if '|' in login and '@' in login:
                    emails.add(login.split('|')[-1].lower())
    return emails


def create_folder(g, s, parent_id, name):
    """returns (id, created). on a 409 the existing folder is looked up instead"""
    resp = g.call('POST', '/drives/%s/items/%s/children' % (s['drive_id'], parent_id), expect=(201, 409), json={
        'name': name, 'folder': {}, '@microsoft.graph.conflictBehavior': 'fail',
    })
    if resp.status_code == 201:
        return resp.json()['id'], True
    found = g.call('GET', child_path(s['drive_id'], parent_id, name) + '?$select=id,folder').json()
    check('folder' in found, '%s exists but is not a folder' % name)
    return found['id'], False


def upload_small(g, s, parent_id, name, data, conflict):
    url = '%s:/content?@microsoft.graph.conflictBehavior=%s' % (child_path(s['drive_id'], parent_id, name), conflict)
    return g.call('PUT', url, expect=(200, 201, 409), data=data, headers={'Content-Type': 'text/plain'})


def create_session(g, s, parent_id, name):
    resp = g.call('POST', '%s:/createUploadSession' % child_path(s['drive_id'], parent_id, name), json={
        'item': {'@microsoft.graph.conflictBehavior': 'fail', 'name': name},
    })
    return resp.json()['uploadUrl']


def upload_chunks(g, upload_url, data):
    total, start, count = len(data), 0, 0
    while start < total:
        end = min(start + CHUNK, total) - 1
        # no Authorization header on the chunk PUTs (graph may answer 401)
        resp = g.call('PUT', upload_url, auth=False, expect=(200, 201, 202), data=data[start:end + 1],
                      headers={'Content-Range': 'bytes %d-%d/%d' % (start, end, total)})
        count += 1
        if resp.status_code in (200, 201):
            return resp.json(), count
        start = end + 1
    raise CheckFailed('all chunks sent but no file came back')


def grant(g, s, email):
    resp = g.call('POST', '/shares/%s/permission/grant' % encode_share(s['link_url']), json={
        'recipients': [{'email': email}], 'roles': ['read'],
    })
    link = next((p for p in resp.json().get('value') or [] if (p.get('link') or {}).get('scope')), None)
    check(link, 'grant returned no link permission')
    changed = link['link'].get('webUrl') != s['link_url']
    s['link_url'] = link['link']['webUrl']
    return changed


def describe(perm):
    link = perm.get('link') or {}
    kind = 'link %s/%s' % (link.get('type'), link.get('scope')) if link else 'direct'
    extra = ' by app' if link.get('application') else ''
    inherited = ' inherited' if perm.get('inheritedFrom') else ''
    return '%s%s%s roles=%s grantees=%d' % (kind, extra, inherited, ','.join(perm.get('roles') or []),
                                           len(link_emails(perm)))


# ----------------------------------------------------------------------
# steps; each returns a short note, raises PocError/CheckFailed on failure
# ----------------------------------------------------------------------

def step_token(g, s):
    g.token()
    try:
        roles = g.token_roles()
    except (ValueError, IndexError, UnicodeDecodeError):
        return 'token received; could not read its roles claim'
    check(roles == ['Sites.Selected'], 'token carries %s, expected only Sites.Selected' % (roles or 'no roles'))
    return 'token received; app permissions in token: Sites.Selected only'


def step_site(g, s):
    site = g.call('GET', '/sites/%s?$select=id,displayName' % s['site_id']).json()
    if s.get('drive_id'):
        drive = g.call('GET', '/drives/%s?$select=id,name,driveType' % s['drive_id']).json()
    else:
        drive = g.call('GET', '/sites/%s/drive?$select=id,name,driveType' % s['site_id']).json()
    s['drive_id'] = drive['id']
    s['root_id'] = g.call('GET', '/drives/%s/root?$select=id' % s['drive_id']).json()['id']
    return 'site "%s", library "%s" (%s)' % (site.get('displayName'), drive.get('name'), drive.get('driveType'))


def step_folders(g, s):
    s['poc_id'], created = create_folder(g, s, s['root_id'], s['poc_name'])
    check(created, '%s already existed' % s['poc_name'])
    intake_id, _ = create_folder(g, s, s['poc_id'], 'Intake')
    s['intake_user_id'], _ = create_folder(g, s, intake_id, s['intake_email'])
    projects_id, _ = create_folder(g, s, s['poc_id'], 'Projects')
    year_id, _ = create_folder(g, s, projects_id, str(datetime.date.today().year))
    s['project_id'], _ = create_folder(g, s, year_id, CODE_FOLDER)
    # creating it again must fail with 409 and the lookup must find the same folder
    again, created = create_folder(g, s, year_id, CODE_FOLDER)
    check(not created and again == s['project_id'], 'second create of %s did not resolve to the same folder' % CODE_FOLDER)
    return 'Intake/<user> and Projects/<year>/%s created; 409 + lookup works' % CODE_FOLDER


def step_small_upload(g, s):
    data = b'Phase 0 proof of concept file.\n'
    resp = upload_small(g, s, s['project_id'], SMALL_NAME, data, 'fail')
    check(resp.status_code in (200, 201), 'upload refused with HTTP %s %s' % (resp.status_code, error_code(resp)))
    s['small_id'] = resp.json()['id']
    again = upload_small(g, s, s['project_id'], SMALL_NAME, data, 'fail')
    check(again.status_code == 409, 'second upload with conflictBehavior=fail was accepted (HTTP %s)' % again.status_code)
    return 'HTTP %s; same name again refused with 409 (%s)' % (resp.status_code, error_code(again))


def step_large_upload(g, s):
    data = os.urandom(LARGE_SIZE)
    item, chunks = upload_chunks(g, create_session(g, s, s['project_id'], LARGE_NAME), data)
    check(item.get('size') == len(data), 'uploaded size %s, expected %d' % (item.get('size'), len(data)))
    s['large_id'] = item['id']
    # a cancelled session must leave nothing behind
    cancel_url = create_session(g, s, s['project_id'], CANCEL_NAME)
    g.call('PUT', cancel_url, auth=False, expect=(202,), data=data[:CHUNK],
           headers={'Content-Range': 'bytes 0-%d/%d' % (CHUNK - 1, len(data))})
    g.call('DELETE', cancel_url, auth=False, expect=(204,))
    left = g.call('GET', child_path(s['drive_id'], s['project_id'], CANCEL_NAME) + '?$select=id', expect=(200, 404))
    check(left.status_code == 404, 'cancelled upload left a file behind')
    return '%.1f MiB in %d chunks; cancelled session left no file (204)' % (len(data) / 1048576.0, chunks)


def step_create_link(g, s):
    url = '/drives/%s/items/%s/createLink' % (s['drive_id'], s['small_id'])
    body = {'type': 'view', 'scope': 'users'}
    first = g.call('POST', url, expect=(200, 201), json=body)
    perm = first.json()
    link = perm.get('link') or {}
    check(link.get('type') == 'view' and link.get('scope') == 'users',
          'got a %s/%s link' % (link.get('type'), link.get('scope')))
    s['link_perm_id'], s['link_url'] = perm['id'], link['webUrl']
    second = g.call('POST', url, expect=(200, 201), json=body)
    note = 'first HTTP %s, repeat HTTP %s, same link: %s' % (
        first.status_code, second.status_code, 'yes' if second.json().get('id') == perm['id'] else 'no')
    check(first.status_code == 201 and second.status_code == 200 and second.json().get('id') == perm['id'], note)
    return note


def step_grant(g, s):
    changed = grant(g, s, s['member_email'])
    return 'member granted read; link URL changed: %s' % ('yes' if changed else 'no')


def step_list_permissions(g, s):
    perms = g.call('GET', '/drives/%s/items/%s/permissions' % (s['drive_id'], s['small_id'])).json().get('value') or []
    for perm in perms:
        print('      - %s' % describe(perm))
    ours = next((p for p in perms if p.get('id') == s['link_perm_id']), None)
    check(ours, "the app's link is not in the list")
    check(s['member_email'] in link_emails(ours), 'member not found on the link (grantees listed: %d)' % len(link_emails(ours)))
    return '%d permission(s); inheritedFrom returned: %s; member on link: yes' % (
        len(perms), 'yes' if any(p.get('inheritedFrom') for p in perms) else 'no')


def step_revoke(g, s):
    url = '/drives/%s/items/%s/permissions/%s/revokeGrants' % (s['drive_id'], s['small_id'], s['link_perm_id'])
    perm = g.call('POST', url, json={'grantees': [{'email': s['member_email']}]}).json()
    check(s['member_email'] not in link_emails(perm), 'member still on the link after revokeGrants')
    # grant again so the member can do the manual check
    changed = grant(g, s, s['member_email'])
    return 'member removed; granted again for the manual check (link URL changed: %s)' % ('yes' if changed else 'no')


def step_resolve_share(g, s):
    item = g.call('GET', '/shares/%s/driveItem?$select=id,name,file,folder,parentReference'
                  % encode_share(s['link_url'])).json()
    parent = item.get('parentReference') or {}
    check(item.get('id') == s['small_id'], 'resolved to a different item')
    check('file' in item and 'folder' not in item, 'not reported as a file')
    check(parent.get('id') == s['project_id'], 'parentReference.id is not the project folder')
    return 'file, parent folder id matches; parentReference.path returned: %s' % ('yes' if parent.get('path') else 'no')


def step_resolve_pasted(g, s):
    item = g.call('GET', '/shares/%s/driveItem?$select=id,name,file,folder,parentReference'
                  % encode_share(s['pasted_link'])).json()
    kind = 'folder' if 'folder' in item else 'file' if 'file' in item else 'unknown'
    parent = item.get('parentReference') or {}
    same_drive = parent.get('driveId') == s['drive_id']
    return '%s "%s", parent path %s, same library: %s' % (kind, item.get('name'), parent.get('path') or '(none)',
                                                          'yes' if same_drive else 'no')


def step_move(g, s):
    resp = upload_small(g, s, s['intake_user_id'], INTAKE_NAME, b'File placed in the intake folder.\n', 'rename')
    check(resp.status_code in (200, 201), 'intake upload refused with HTTP %s %s' % (resp.status_code, error_code(resp)))
    intake_file = resp.json()['id']
    # a link like the ones "copy link" makes; the delete step must remove it after the move
    g.call('POST', '/drives/%s/items/%s/createLink' % (s['drive_id'], intake_file), expect=(200, 201),
           json={'type': 'view', 'scope': 'users'})
    moved = g.call('PATCH', '/drives/%s/items/%s' % (s['drive_id'], intake_file), json={
        'parentReference': {'id': s['project_id']}, 'name': MOVED_NAME,
    }).json()
    check(moved.get('name') == MOVED_NAME, 'name after move is %s' % moved.get('name'))
    check((moved.get('parentReference') or {}).get('id') == s['project_id'], 'not in the project folder after the move')
    s['moved_id'] = moved['id']
    return 'moved and renamed to %s; item id kept: %s' % (MOVED_NAME, 'yes' if moved['id'] == intake_file else 'no')


def step_delete_link(g, s):
    url = '/drives/%s/items/%s/permissions' % (s['drive_id'], s['moved_id'])
    links = [p for p in g.call('GET', url).json().get('value') or [] if p.get('link')]
    check(links, 'no link permission found on the moved file')
    for perm in links:
        g.call('DELETE', '%s/%s' % (url, perm['id']), expect=(204,))
    left = [p for p in g.call('GET', url).json().get('value') or [] if p.get('link')]
    check(not left, '%d link(s) still there after DELETE' % len(left))
    return 'deleted %d link(s) on the moved file; none left' % len(links)


def step_invite(g, s):
    perms = g.call('POST', '/drives/%s/items/%s/invite' % (s['drive_id'], s['intake_user_id']), json={
        'recipients': [{'email': s['intake_email']}],
        'roles': ['write'],
        'requireSignIn': True,
        'sendInvitation': False,
    }).json().get('value') or []
    check(perms, 'invite returned no permission')
    check(all('write' in (p.get('roles') or []) for p in perms), 'roles returned: %s' % [p.get('roles') for p in perms])
    return '%d permission(s) with write on Intake/<user>' % len(perms)


STEPS = [
    ('1  token', step_token, ()),
    ('2  site and library', step_site, ()),
    ('3  create folders (+409)', step_folders, ('drive_id',)),
    ('4  small upload (PUT)', step_small_upload, ('project_id',)),
    ('5  large upload (session + cancel)', step_large_upload, ('project_id',)),
    ('6  createLink view/users', step_create_link, ('small_id',)),
    ('7  grant', step_grant, ('link_url',)),
    ('8  list permissions', step_list_permissions, ('link_perm_id',)),
    ('9  revokeGrants', step_revoke, ('link_perm_id',)),
    ('10 /shares resolve (own link)', step_resolve_share, ('link_url',)),
    ('11 /shares resolve (pasted link)', step_resolve_pasted, ('pasted_link',)),
    ('12 move + rename (PATCH)', step_move, ('intake_user_id', 'project_id')),
    ('13 delete link permission', step_delete_link, ('moved_id',)),
    ('14 invite on intake folder', step_invite, ('intake_user_id',)),
]


def run_step(name, fn, needs, g, s, results):
    missing = [key for key in needs if not s.get(key)]
    if missing:
        status, note = 'SKIP', 'needs %s' % ', '.join(missing)
    else:
        try:
            status, note = 'PASS', fn(g, s) or ''
        except PocError as e:
            status, note = 'FAIL', 'HTTP %s %s at %s' % (e.status, e.code or '(no code)', e.where)
        except CheckFailed as e:
            status, note = 'FAIL', str(e)
        except requests.RequestException as e:
            status, note = 'FAIL', 'network: %s' % type(e).__name__
        except (KeyError, ValueError) as e:
            status, note = 'FAIL', 'unexpected response: %s %s' % (type(e).__name__, e)
    results.append((name, status, note))
    print('  [%s] %-36s %s' % (status, name, note))


def read_config():
    env = os.environ
    s = {
        'tenant': env.get('DMS_TENANT_ID', '').strip(),
        'client_id': env.get('DMS_CLIENT_ID', '').strip(),
        'secret': env.get('DMS_CLIENT_SECRET', ''),
        'site_id': env.get('DMS_SITE_ID', '').strip(),
        'drive_id': env.get('DMS_DRIVE_ID', '').strip(),
        'member_email': env.get('DMS_TEST_MEMBER_EMAIL', '').strip().lower(),
        'pasted_link': env.get('DMS_TEST_PASTED_LINK', '').strip(),
    }
    s['intake_email'] = env.get('DMS_TEST_INTAKE_EMAIL', '').strip().lower() or s['member_email']
    names = {'tenant': 'DMS_TENANT_ID', 'client_id': 'DMS_CLIENT_ID', 'secret': 'DMS_CLIENT_SECRET',
             'site_id': 'DMS_SITE_ID', 'member_email': 'DMS_TEST_MEMBER_EMAIL'}
    missing = [var for key, var in names.items() if not s[key]]
    if missing:
        sys.exit('missing environment variables: %s' % ', '.join(missing))
    return s


def cleanup(g, s, name):
    if not name.startswith('PoC-') or '/' in name or '\\' in name:
        sys.exit('refusing to delete "%s": only a PoC-<timestamp> folder at the library root can be removed' % name)
    step_site(g, s)
    item = g.call('GET', '/drives/%s/root:/%s?$select=id,name,folder' % (s['drive_id'], quote(name)), expect=(200, 404))
    if item.status_code == 404:
        sys.exit('%s not found' % name)
    check('folder' in item.json(), '%s is not a folder' % name)
    g.call('DELETE', '/drives/%s/items/%s' % (s['drive_id'], item.json()['id']), expect=(204,))
    print('deleted %s (it goes to the site recycle bin)' % name)


def main():
    parser = argparse.ArgumentParser(description='microsoft graph proof of concept for document_management_system')
    parser.add_argument('--cleanup', metavar='POC_FOLDER', help='delete a PoC-<timestamp> folder made by an earlier run')
    args = parser.parse_args()

    s = read_config()
    g = Graph(s['tenant'], s['client_id'], s['secret'])
    if args.cleanup:
        try:
            cleanup(g, s, args.cleanup)
        except (PocError, CheckFailed) as e:
            sys.exit('cleanup failed: %s' % e)
        return

    s['poc_name'] = 'PoC-%s' % datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    print('phase 0 proof of concept, test folder %s' % s['poc_name'])
    results = []
    for name, fn, needs in STEPS:
        run_step(name, fn, needs, g, s, results)

    failed = [r for r in results if r[1] == 'FAIL']
    print('\n%d passed, %d failed, %d skipped' % (
        sum(1 for r in results if r[1] == 'PASS'), len(failed), sum(1 for r in results if r[1] == 'SKIP')))
    if s.get('poc_id'):
        if s.get('link_url'):
            print('\nmanual check, signed in as %s, open:\n  %s' % (s['member_email'], s['link_url']))
            print('note whether the folder path (%s/Projects/<year>/%s) shows anywhere: breadcrumb, details pane,'
                  ' "open file location". then open the same link as another user: access must be refused.'
                  % (s['poc_name'], CODE_FOLDER))
        print('\ntest folder kept. remove it with:\n  python scripts/poc_graph.py --cleanup %s' % s['poc_name'])
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
