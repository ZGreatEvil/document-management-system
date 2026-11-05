# -*- coding: utf-8 -*-
import logging
import mimetypes
import os

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools.mimetypes import guess_mimetype

from . import sharepoint
from .graph_client import DmsGraphError, GraphClient, get_settings

_logger = logging.getLogger(__name__)

MANAGER_GROUP = 'document_management_system.group_dms_manager'

# where the file lives in sharepoint and who may open it: set only by the module's own methods (sudo)
PROTECTED_FIELDS = {
    'project_id', 'number_id', 'name', 'original_filename', 'source', 'drive_id', 'item_id',
    'link_permission_id', 'share_url', 'size', 'mimetype', 'uploaded_by_id', 'uploaded_on', 'state', 'last_error',
}


class DmsFile(models.Model):
    _name = 'dms.file'
    _description = 'Project File'
    _inherit = ['mail.thread']
    _order = 'uploaded_on desc, id desc'

    name = fields.Char(string='File Name', required=True, readonly=True,
                       help='The file name in SharePoint.')
    project_id = fields.Many2one('project.project', string='Project', required=True, index=True,
                                 ondelete='restrict', readonly=True)
    company_id = fields.Many2one(related='project_id.company_id', store=True, index=True)
    unique_code_id = fields.Many2one(related='project_id.dms_unique_code_id', store=True, index=True,
                                     string='Unique Code')
    number_id = fields.Many2one(
        'doc.number', string='Document Number', index=True, ondelete='restrict', readonly=True,
        domain="[('unique_code_id', '=', unique_code_id), ('state', '=', 'active')]")
    original_filename = fields.Char(string='Original File Name', readonly=True)
    source = fields.Selection([('upload', 'Uploaded in Odoo'), ('link', 'Moved from an upload folder')],
                              required=True, readonly=True)
    drive_id = fields.Char(string='Drive ID', readonly=True, groups=MANAGER_GROUP)
    item_id = fields.Char(string='Item ID', readonly=True, index=True, groups=MANAGER_GROUP)
    link_permission_id = fields.Char(string='Link Permission ID', readonly=True, groups=MANAGER_GROUP)
    share_url = fields.Char(string='Link', readonly=True,
                            help='The specific-people link. SharePoint refuses anyone who is not a project member.')
    # bytes; a float column so files above 2 GiB (moved from an upload folder) still fit
    size = fields.Float(digits=(20, 0), readonly=True)
    mimetype = fields.Char(string='File Type', readonly=True)
    uploaded_by_id = fields.Many2one('res.users', string='Uploaded By', readonly=True,
                                     default=lambda self: self.env.user)
    uploaded_on = fields.Datetime(string='Uploaded On', readonly=True, default=fields.Datetime.now)
    state = fields.Selection(
        [('shared', 'Shared'), ('superseded', 'Superseded'), ('error', 'Error'), ('removed', 'Removed')],
        default='shared', required=True, readonly=True, tracking=True,
        help='Superseded: a newer file exists for the same document number.')
    last_error = fields.Text(readonly=True, groups=MANAGER_GROUP)
    grant_ids = fields.One2many('dms.file.grant', 'file_id', string='Access Given', groups=MANAGER_GROUP)

    def init(self):
        # one shared file per document number, also under concurrent uploads
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS dms_file_number_shared_uniq
            ON dms_file (number_id) WHERE state = 'shared' AND number_id IS NOT NULL
        """)

    def _check_number_free(self, number_id):
        """checked before saving, so the user gets this message rather than the unique index's error"""
        taken = self.sudo().search([('number_id', '=', number_id), ('state', '=', 'shared'),
                                    ('id', 'not in', self.ids)], limit=1)
        if taken:
            raise ValidationError(_('%s already has a shared file. Mark the old one as superseded first.')
                                  % taken.number_id.display_number)

    @api.constrains('number_id', 'project_id')
    def _check_number_project(self):
        for file in self.sudo().filtered('number_id'):
            if file.number_id.unique_code_id != file.project_id.dms_unique_code_id:
                raise ValidationError(_('%s does not belong to this project\'s unique code.')
                                      % file.number_id.display_number)

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            raise AccessError(_('Add files with the "Add file" button on the project.'))
        for vals in vals_list:
            if vals.get('number_id') and vals.get('state', 'shared') == 'shared':
                self.browse()._check_number_free(vals['number_id'])
        return super().create(vals_list)

    def write(self, vals):
        if PROTECTED_FIELDS & set(vals) and not self.env.su:
            raise AccessError(_('Where a file lives and who may open it can only be changed by the system.'))
        if vals.get('number_id') or vals.get('state') == 'shared':
            for file in self:
                number_id = vals.get('number_id', file.number_id.id)
                if number_id and vals.get('state', file.state) == 'shared':
                    file._check_number_free(number_id)
        return super().write(vals)

    def unlink(self):
        raise UserError(_('Project files are never deleted. A Document Management administrator can remove '
                          'a file\'s link instead.'))

    def copy(self, default=None):
        raise UserError(_('Project files cannot be duplicated.'))

    # ------------------------------------------------------------------
    # sharepoint
    # ------------------------------------------------------------------

    @api.model
    def _dms_client(self):
        """every graph call of the module starts here (tests replace it)"""
        return GraphClient.from_env(self.env)

    @api.model
    def _dms_error_message(self, error):
        if error.code == 'notConfigured':
            return _('SharePoint is not set up yet: the connection settings or the client secret are missing.')
        if error.status == 0:
            return _('SharePoint could not be reached. Try again in a moment.')
        if error.status in (401, 403):
            return _('SharePoint refused access (%s). An administrator checks the app\'s access to the site.') % error.code
        return _('SharePoint answered with an error (%s %s). Try again, or ask an administrator.') % (
            error.status, error.code)

    @api.model
    def _dms_add(self, project, data, original_filename, number=None, supersede=None):
        """7.2: uploads one file into the project folder, shares it with the members and records it.
        when sharepoint fails nothing is saved in odoo, and a file already uploaded is removed again"""
        project = project.sudo()
        folder_id = project._dms_ensure_folder()
        drive_id = get_settings(self.env)['drive_id']
        if number:
            name, conflict = sharepoint.number_filename(number.display_number, original_filename), 'fail'
        else:
            name, conflict = sharepoint.clean_filename(original_filename), 'rename'
        name = sharepoint.fit_length(name, project._dms_folder_path())
        mimetype = mimetypes.guess_type(original_filename or '')[0] or guess_mimetype(data)
        emails = {user: user._dms_email() for user in project.dms_member_user_ids}
        client = item = None
        try:
            client = self._dms_client()
            item = sharepoint.upload_file(client, drive_id, folder_id, name, data, conflict, mimetype)
            permission_id, share_url = sharepoint.share_with(client, drive_id, item['id'], sorted(set(emails.values())))
            if supersede:
                supersede.sudo().write({'state': 'superseded'})
            file = self.sudo().create({
                'name': item.get('name') or name,
                'project_id': project.id,
                'number_id': number.id if number else False,
                'original_filename': original_filename,
                'source': 'upload',
                'drive_id': drive_id,
                'item_id': item['id'],
                'link_permission_id': permission_id,
                'share_url': share_url,
                'size': item.get('size') or len(data),
                'mimetype': mimetype,
                'uploaded_by_id': self.env.uid,
            })
            self.env['dms.file.grant'].sudo().create([
                {'file_id': file.id, 'user_id': user.id, 'email': email} for user, email in emails.items()])
        except DmsGraphError as e:
            if item:
                sharepoint.delete_item(client, drive_id, item['id'])
            if number and e.status == 409:
                raise UserError(_('%s is already in the project folder. If this is a new version of the document, '
                                  'add a revision to the number first.') % name)
            raise UserError(self._dms_error_message(e))
        except Exception:
            if item:
                sharepoint.delete_item(client, drive_id, item['id'])
            raise
        return file

    @api.model
    def _dms_resolve_intake_link(self, client, url):
        """the file behind a pasted link, refused unless it is a file directly in the user's own upload folder,
        in the configured library, and not added before"""
        url = (url or '').strip()
        if not url.lower().startswith('https://'):
            raise UserError(_('Paste the link you copied in SharePoint. It starts with https://'))
        intake_id = self.env.user.sudo().dms_intake_item_id
        if not intake_id:
            raise UserError(_('Open "My upload folder" first, put the file there, then copy the file\'s link.'))
        try:
            item = client.get('/shares/%s/driveItem?$select=id,name,size,file,folder,parentReference'
                              % sharepoint.encode_share_url(url))
        except DmsGraphError as e:
            if e.status in (400, 403, 404):
                raise UserError(_('This link does not lead to a file Odoo can open. Copy the link of the file in '
                                  'your upload folder again.'))
            raise UserError(self._dms_error_message(e))
        name = item.get('name') or _('This item')
        if 'folder' in item or 'file' not in item:
            raise UserError(_('%s is a folder. Copy the link of the file itself, not of a folder.') % name)
        if self.sudo().search_count([('item_id', '=', item['id'])]):
            raise UserError(_('%s has already been added to a project.') % name)
        parent = item.get('parentReference') or {}
        if parent.get('driveId') and parent['driveId'] != get_settings(self.env)['drive_id']:
            raise UserError(_('%s is not in the project document library. Put it in your upload folder first.') % name)
        if parent.get('id') != intake_id:
            raise UserError(_('%s is not in your upload folder. Move it into your upload folder (not into a folder '
                              'inside it), then copy its link again.') % name)
        return item

    @api.model
    def _dms_add_from_link(self, project, url, number=None, supersede=None):
        """7.3: moves a file from the user's own upload folder into the project folder, renamed, removes the links
        others made on it, then shares and records it like an upload. when a step fails the file goes back to the
        upload folder under its own name and nothing is saved in odoo"""
        project = project.sudo()
        folder_id = project._dms_ensure_folder()
        drive_id = get_settings(self.env)['drive_id']
        intake_id = self.env.user.sudo().dms_intake_item_id
        try:
            client = self._dms_client()
        except DmsGraphError as e:
            raise UserError(self._dms_error_message(e))
        item = self._dms_resolve_intake_link(client, url)
        original = item.get('name') or 'file'
        if number:
            name = sharepoint.number_filename(number.display_number, original)
        else:
            name = sharepoint.clean_filename(original)
        name = sharepoint.fit_length(name, project._dms_folder_path())
        emails = {user: user._dms_email() for user in project.dms_member_user_ids}
        moved = None
        try:
            moved = sharepoint.move_item(client, drive_id, item['id'], folder_id, name, rename=not number)
            sharepoint.remove_links(client, drive_id, item['id'])
            uploader_email = self.env.user._dms_email()
            if uploader_email:
                sharepoint.remove_direct_access(client, drive_id, item['id'], uploader_email)
            permission_id, share_url = sharepoint.share_with(client, drive_id, item['id'], sorted(set(emails.values())))
            if supersede:
                supersede.sudo().write({'state': 'superseded'})
            file = self.sudo().create({
                'name': moved.get('name') or name,
                'project_id': project.id,
                'number_id': number.id if number else False,
                'original_filename': original,
                'source': 'link',
                'drive_id': drive_id,
                'item_id': item['id'],
                'link_permission_id': permission_id,
                'share_url': share_url,
                'size': item.get('size') or 0,
                'mimetype': (item.get('file') or {}).get('mimeType') or mimetypes.guess_type(original)[0],
                'uploaded_by_id': self.env.uid,
            })
            self.env['dms.file.grant'].sudo().create([
                {'file_id': file.id, 'user_id': user.id, 'email': email} for user, email in emails.items()])
        except DmsGraphError as e:
            if moved:
                self._dms_undo_move(client, drive_id, item['id'], intake_id, original)
            if number and e.status == 409 and not moved:
                raise UserError(_('%s is already in the project folder. If this is a new version of the document, '
                                  'add a revision to the number first.') % name)
            raise UserError(self._dms_error_message(e))
        except Exception:
            if moved:
                self._dms_undo_move(client, drive_id, item['id'], intake_id, original)
            raise
        return file

    @api.model
    def _dms_undo_move(self, client, drive_id, item_id, intake_id, original):
        """the user's file goes back where it was, without any link (odoo's may exist even when the grant
        failed); it is never deleted"""
        try:
            sharepoint.remove_links(client, drive_id, item_id)
        except DmsGraphError as e:
            _logger.warning('graph: links on a file could not be removed: HTTP %s %s', e.status, e.code)
        try:
            sharepoint.move_item(client, drive_id, item_id, intake_id, original, rename=True)
        except DmsGraphError as e:
            _logger.warning('graph: a file could not be moved back to its upload folder: HTTP %s %s',
                            e.status, e.code)

    def action_open(self):
        self.ensure_one()
        if self.state == 'removed' or not self.share_url:
            raise UserError(_('This file has no link any more.'))
        return {'type': 'ir.actions.act_url', 'url': self.share_url, 'target': 'new'}

    # ------------------------------------------------------------------
    # access upkeep (7.4, 7.6)
    # ------------------------------------------------------------------

    def _dms_sync_grants(self, client, wanted):
        """one file: people who left are revoked, missing members are granted. wanted is {user id: email}.
        the grant rows follow what sharepoint was told"""
        self.ensure_one()
        file = self.sudo()
        Grant = self.env['dms.file.grant'].sudo()
        current = {grant.user_id.id: grant for grant in file.grant_ids}
        add = {uid: email for uid, email in wanted.items()
               if email and (uid not in current or current[uid].email != email)}
        remove = Grant.browse([grant.id for uid, grant in current.items()
                               if uid not in wanted or wanted[uid] != grant.email])
        if remove:
            client.post('/drives/%s/items/%s/permissions/%s/revokeGrants'
                        % (file.drive_id, file.item_id, file.link_permission_id), expect=(200,),
                        json={'grantees': [{'email': email} for email in sorted(set(remove.mapped('email')))]})
            remove.unlink()
        if add:
            result = client.post('/shares/%s/permission/grant' % sharepoint.encode_share_url(file.share_url),
                                 expect=(200,), json={'recipients': [{'email': e} for e in sorted(set(add.values()))],
                                                      'roles': ['read']})
            updated = next((p for p in result.get('value') or [] if (p.get('link') or {}).get('scope')), None)
            if updated and updated['link'].get('webUrl') and updated['link']['webUrl'] != file.share_url:
                file.write({'share_url': updated['link']['webUrl']})
            Grant.create([{'file_id': file.id, 'user_id': uid, 'email': email} for uid, email in add.items()])
        return bool(add or remove)

    def _dms_relink(self, client, wanted):
        """odoo's link on the file has gone: a new one is made and given to the members"""
        self.ensure_one()
        file = self.sudo()
        permission_id, share_url = sharepoint.share_with(client, file.drive_id, file.item_id,
                                                         sorted(set(e for e in wanted.values() if e)))
        file.grant_ids.unlink()
        file.write({'link_permission_id': permission_id, 'share_url': share_url, 'last_error': False})
        self.env['dms.file.grant'].sudo().create([
            {'file_id': file.id, 'user_id': uid, 'email': email} for uid, email in wanted.items() if email])

    def _dms_delete_for_cancelled_number(self, client):
        """the file's number was cancelled: the file is deleted in sharepoint (it goes to the site's recycle bin);
        odoo keeps the record, marked removed, as history"""
        self.ensure_one()
        file = self.sudo()
        try:
            client.delete('/drives/%s/items/%s' % (file.drive_id, file.item_id))
        except DmsGraphError as e:
            if e.status != 404:
                raise
        file.grant_ids.unlink()
        file.write({'state': 'removed', 'last_error': False})
        file.message_post(body=_('Deleted from SharePoint because %s was cancelled. It can be restored from the '
                                 'site\'s recycle bin by a site owner.') % file.number_id.display_number)

    def action_dms_remove(self):
        """7.6, DMS managers: the file's links are deleted so nobody can open it through odoo any more.
        the file itself stays in sharepoint (site owners can delete it by hand)"""
        if not self.env.user.has_group(MANAGER_GROUP):
            raise AccessError(_('Only Document Management administrators can remove files.'))
        files = self.sudo().filtered(lambda f: f.state != 'removed')
        if not files:
            return True
        try:
            client = self._dms_client()
            for file in files:
                name = file.name
                try:
                    sharepoint.remove_links(client, file.drive_id, file.item_id)
                    # renamed so its name is free again, e.g. for the right file of the same number
                    stem, ext = os.path.splitext(file.name)
                    moved = sharepoint.move_item(
                        client, file.drive_id, file.item_id, file.project_id._dms_ensure_folder(),
                        '%s (removed %s)%s' % (stem, fields.Date.context_today(self), ext), rename=True)
                    name = moved.get('name') or name
                except DmsGraphError as e:
                    # a file already deleted in sharepoint has nothing left to remove
                    if e.status != 404:
                        raise
                file.grant_ids.unlink()
                file.write({'state': 'removed', 'name': name, 'last_error': False})
                file.message_post(body=_('Removed by %s: its link was deleted, so nobody can open it from Odoo. '
                                         'The file is still in SharePoint.') % self.env.user.name)
        except DmsGraphError as e:
            raise UserError(_('The file could not be removed. %s') % self._dms_error_message(e))
        return True


class DmsFileGrant(models.Model):
    """what odoo last told sharepoint: who is on a file's link. the member sync works out the difference"""
    _name = 'dms.file.grant'
    _description = 'Project File Access'
    _order = 'file_id, user_id'

    file_id = fields.Many2one('dms.file', required=True, index=True, ondelete='cascade', readonly=True)
    # restrict: a deleted user's grant must still be revoked in sharepoint first
    user_id = fields.Many2one('res.users', required=True, index=True, ondelete='restrict', readonly=True)
    email = fields.Char(required=True, readonly=True)
    granted_on = fields.Datetime(readonly=True, default=fields.Datetime.now)

    _sql_constraints = [
        ('file_user_uniq', 'unique(file_id, user_id)', 'This user already has access to this file.'),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            raise AccessError(_('File access is kept up to date by the system.'))
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.su:
            raise AccessError(_('File access is kept up to date by the system.'))
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            raise AccessError(_('File access is kept up to date by the system.'))
        return super().unlink()
