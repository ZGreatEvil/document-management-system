# -*- coding: utf-8 -*-
from odoo import _, fields, models
from odoo.exceptions import AccessError, UserError

from . import sharepoint
from .graph_client import DmsGraphError, get_settings

MANAGER_GROUP = 'document_management_system.group_dms_manager'


class ResUsers(models.Model):
    _inherit = 'res.users'

    # not in SELF_WRITEABLE_FIELDS: users cannot change their own
    dms_m365_email = fields.Char(
        string='Microsoft 365 Sign-in', groups=MANAGER_GROUP, copy=False,
        help='The address this user signs in to Microsoft 365 with. Empty: the login is used.')
    dms_intake_item_id = fields.Char(string='Upload Folder ID', readonly=True, copy=False, groups=MANAGER_GROUP)

    def _dms_email(self):
        """the address file access is given to, or False when the user has none"""
        self.ensure_one()
        user = self.sudo()
        email = (user.dms_m365_email or user.login or '').strip().lower()
        return email if '@' in email else False

    def write(self, vals):
        if 'dms_intake_item_id' in vals and not self.env.su:
            raise AccessError(_('The upload folder is set by the system.'))
        return super().write(vals)

    def _dms_intake_folder(self):
        """7.5: the user's own upload folder Intake/<address>/, as (id, web url). on first use, or when it
        has gone, it is created and only this user gets write access (no email is sent)"""
        self.ensure_one()
        user = self.sudo()
        email = user._dms_email()
        if not email:
            raise UserError(_('Your user has no Microsoft 365 address. Ask a Document Management administrator to '
                              'set it on your user.'))
        settings = get_settings(self.env)
        if not (settings['drive_id'] and settings['intake_folder_id']):
            raise UserError(_('SharePoint is not set up yet. An administrator fills in the document library and the '
                              '"Intake" folder in Settings > Document Management.'))
        drive_id = settings['drive_id']
        File = self.env['dms.file']
        try:
            client = File._dms_client()
            if user.dms_intake_item_id:
                try:
                    folder = client.get('/drives/%s/items/%s?$select=id,webUrl,folder'
                                        % (drive_id, user.dms_intake_item_id))
                    if 'folder' in folder:
                        return folder['id'], folder.get('webUrl')
                except DmsGraphError as e:
                    if e.status != 404:
                        raise
            folder_id = sharepoint.ensure_folder(client, drive_id, settings['intake_folder_id'],
                                                 sharepoint.clean_filename(email))
            client.post('/drives/%s/items/%s/invite' % (drive_id, folder_id), expect=(200,), json={
                'recipients': [{'email': email}],
                'roles': ['write'],
                'requireSignIn': True,
                'sendInvitation': False,
            })
            folder = client.get('/drives/%s/items/%s?$select=id,webUrl,folder' % (drive_id, folder_id))
        except DmsGraphError as e:
            raise UserError(_('Your upload folder could not be prepared. %s') % File._dms_error_message(e))
        user.write({'dms_intake_item_id': folder_id})
        return folder_id, folder.get('webUrl')

    def action_dms_upload_folder(self):
        """opens the current user's upload folder in sharepoint"""
        _folder_id, url = self.env.user._dms_intake_folder()
        if not url:
            raise UserError(_('SharePoint did not return the folder\'s address. Try again in a moment.'))
        return {'type': 'ir.actions.act_url', 'url': url, 'target': 'new'}
