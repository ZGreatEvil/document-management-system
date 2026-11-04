# -*- coding: utf-8 -*-
"""sharepoint names and the graph steps the flows share. plain functions over a GraphClient."""
import logging
import os
import re
from urllib.parse import quote

from .graph_client import UPLOAD_CHUNK, DmsGraphError, encode_share_url

_logger = logging.getLogger(__name__)

# simple upload up to this size, an upload session above it (microsoft recommends sessions above 10 MiB)
SIMPLE_UPLOAD_LIMIT = 10 * 1024 * 1024
# sharepoint allows 400 characters for the folder path plus the file name; odoo only knows the part from
# the "Projects" folder down, so this much is kept free for the site and library part
MAX_PATH = 400
SITE_PATH_MARGIN = 100

FORBIDDEN_RE = re.compile(r'["*:<>?/\\|#%\x00-\x1f]')
VTI_RE = re.compile('_vti_', re.IGNORECASE)
RESERVED = ({'.lock', 'con', 'prn', 'aux', 'nul', 'desktop.ini'}
            | {'com%d' % i for i in range(10)} | {'lpt%d' % i for i in range(10)})


def clean_filename(name, default='file'):
    """a name sharepoint accepts: forbidden characters become _, no leading or trailing spaces,
    no reserved names, nothing starting with ~$ and no _vti_"""
    name = FORBIDDEN_RE.sub('_', name or '').strip()
    name = VTI_RE.sub('_vti-', name)
    if name.startswith('~$'):
        name = '_' + name[2:]
    stem, ext = os.path.splitext(name)
    stem = stem.strip() or default
    if stem.lower() in RESERVED or (stem + ext).lower() in RESERVED:
        stem = '_' + stem
    return stem + ext.strip()


def number_filename(display_number, original_filename):
    """QTN-001/SDX001/XI/26 Rev.1 + report.pdf -> QTN-001_SDX001_XI_26_Rev.1.pdf"""
    ext = FORBIDDEN_RE.sub('', os.path.splitext(original_filename or '')[1]).strip()
    return clean_filename(display_number.replace('/', '_').replace(' ', '_') + ext)


def fit_length(name, folder_path):
    """shortens the name (never the extension) so folder path + name stays inside sharepoint's limit"""
    room = MAX_PATH - SITE_PATH_MARGIN - len(folder_path)
    if len(name) <= room:
        return name
    stem, ext = os.path.splitext(name)
    return stem[:max(room - len(ext), 1)].rstrip() + ext


def child_path(drive_id, parent_id, name):
    return '/drives/%s/items/%s:/%s' % (drive_id, parent_id, quote(name))


def ensure_folder(client, drive_id, parent_id, name):
    """the id of folder `name` under parent, created when missing"""
    try:
        return client.post('/drives/%s/items/%s/children' % (drive_id, parent_id), expect=(201,), json={
            'name': name, 'folder': {}, '@microsoft.graph.conflictBehavior': 'fail',
        })['id']
    except DmsGraphError as e:
        if e.status != 409:
            raise
    found = client.get(child_path(drive_id, parent_id, name) + '?$select=id,folder')
    if 'folder' not in found:
        raise DmsGraphError(409, 'notAFolder')
    return found['id']


def upload_file(client, drive_id, parent_id, name, data, conflict, mimetype='application/octet-stream'):
    """uploads data as `name` into the folder; conflict is fail or rename. returns the driveItem"""
    path = child_path(drive_id, parent_id, name)
    if len(data) <= SIMPLE_UPLOAD_LIMIT:
        return client.put('%s:/content?@microsoft.graph.conflictBehavior=%s' % (path, conflict), data,
                          content_type=mimetype)
    upload_url = client.post('%s:/createUploadSession' % path, expect=(200,), json={
        'item': {'@microsoft.graph.conflictBehavior': conflict, 'name': name},
    })['uploadUrl']
    total = len(data)
    try:
        for start in range(0, total, UPLOAD_CHUNK):
            resp = client.upload_chunk(upload_url, data[start:start + UPLOAD_CHUNK], start, total)
            if resp.status_code in (200, 201):
                return resp.json()
    except DmsGraphError:
        cancel_upload(client, upload_url)
        raise
    cancel_upload(client, upload_url)
    raise DmsGraphError(0, 'uploadIncomplete')


def cancel_upload(client, upload_url):
    try:
        client.request('DELETE', upload_url, auth=False, expect=(204, 404))
    except DmsGraphError:
        _logger.warning('graph: an unfinished upload session could not be cancelled; it expires by itself')


def share_with(client, drive_id, item_id, emails):
    """the file's view-only, specific-people link, granted to these addresses.
    returns (link permission id, share url); grant may change the url, so its answer wins"""
    perm = client.post('/drives/%s/items/%s/createLink' % (drive_id, item_id), json={'type': 'view', 'scope': 'users'})
    link = perm.get('link') or {}
    if link.get('type') != 'view' or link.get('scope') != 'users' or not link.get('webUrl'):
        raise DmsGraphError(0, 'unexpectedLink')
    url = link['webUrl']
    if emails:
        result = client.post('/shares/%s/permission/grant' % encode_share_url(url), expect=(200,), json={
            'recipients': [{'email': email} for email in emails], 'roles': ['read'],
        })
        updated = next((p for p in result.get('value') or [] if (p.get('link') or {}).get('scope')), None)
        if updated and updated['link'].get('webUrl'):
            url = updated['link']['webUrl']
    return perm['id'], url


def move_item(client, drive_id, item_id, parent_id, name, rename=False):
    """moves and renames in one PATCH. graph documents no conflict option for this, so with rename=True
    a taken name becomes 'name 1', 'name 2', ...; without it a taken name raises the 409"""
    stem, ext = os.path.splitext(name)
    tries = 10 if rename else 1
    for attempt in range(tries):
        candidate = name if not attempt else '%s %d%s' % (stem, attempt, ext)
        try:
            return client.patch('/drives/%s/items/%s' % (drive_id, item_id), json={
                'parentReference': {'id': parent_id}, 'name': candidate,
            })
        except DmsGraphError as e:
            if e.status != 409 or attempt == tries - 1:
                raise


def list_permissions(client, drive_id, item_id):
    url = '/drives/%s/items/%s/permissions' % (drive_id, item_id)
    perms = []
    while url:
        page = client.get(url)
        perms.extend(page.get('value') or [])
        url = page.get('@odata.nextLink')
    return perms


def remove_links(client, drive_id, item_id, keep=()):
    """deletes the sharing links on an item (made with "copy link" or otherwise), except the ids in keep.
    sharepoint lists inherited permissions without saying so, so nothing but links is touched;
    "existing access" links give nothing and are left alone. returns how many were deleted"""
    removed = 0
    for perm in list_permissions(client, drive_id, item_id):
        link = perm.get('link') or {}
        if not link or link.get('scope') == 'existingAccess' or perm['id'] in keep:
            continue
        client.delete('/drives/%s/items/%s/permissions/%s' % (drive_id, item_id, perm['id']))
        removed += 1
    return removed


def delete_item(client, drive_id, item_id):
    """clean-up after a failed add (best effort: the error that caused it is what the user sees)"""
    try:
        client.delete('/drives/%s/items/%s' % (drive_id, item_id))
    except DmsGraphError as e:
        _logger.warning('graph: clean-up of an uploaded file failed: HTTP %s %s', e.status, e.code)
