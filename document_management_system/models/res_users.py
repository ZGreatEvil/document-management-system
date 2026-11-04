# -*- coding: utf-8 -*-
from odoo import _, fields, models
from odoo.exceptions import AccessError

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
