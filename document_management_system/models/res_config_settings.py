# -*- coding: utf-8 -*-
from odoo import _, fields, models
from odoo.exceptions import UserError
from odoo.tools import config

from .graph_client import PARAM_PREFIX, SECRET_KEY, DmsGraphError, GraphClient


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    dms_tenant_id = fields.Char(string='Tenant ID', config_parameter=PARAM_PREFIX + 'tenant_id')
    dms_client_id = fields.Char(string='Application (Client) ID', config_parameter=PARAM_PREFIX + 'client_id')
    dms_site_id = fields.Char(string='Site ID', config_parameter=PARAM_PREFIX + 'site_id')
    dms_drive_id = fields.Char(string='Document Library (Drive) ID', config_parameter=PARAM_PREFIX + 'drive_id')
    dms_projects_folder_id = fields.Char(string='"Projects" Folder ID',
                                         config_parameter=PARAM_PREFIX + 'projects_folder_id')
    dms_intake_folder_id = fields.Char(string='"Intake" Folder ID', config_parameter=PARAM_PREFIX + 'intake_folder_id')
    # only whether the secret is there, never its value
    dms_secret_set = fields.Boolean(string='Client Secret in odoo.conf', compute='_compute_dms_secret_set')

    def _compute_dms_secret_set(self):
        for settings in self:
            settings.dms_secret_set = bool(config.get(SECRET_KEY))

    def action_dms_test_connection(self):
        """tests the values on screen (saved or not): gets a token and reads the site"""
        self.ensure_one()
        if not (self.dms_tenant_id and self.dms_client_id and self.dms_site_id):
            raise UserError(_('Fill in the tenant ID, application ID and site ID first.'))
        if not config.get(SECRET_KEY):
            raise UserError(_('The client secret is missing: add %s to odoo.conf and restart Odoo.') % SECRET_KEY)
        try:
            client = GraphClient.from_env(self.env, tenant_id=self.dms_tenant_id.strip(),
                                          client_id=self.dms_client_id.strip())
            site = client.test_connection(self.dms_site_id.strip())
        except DmsGraphError as e:
            raise UserError(_('Microsoft 365 refused the connection (HTTP %(status)s %(code)s). Check the IDs, '
                              'the client secret and the app\'s access to the site.',
                              status=e.status or '-', code=e.code))
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Connected'),
                'message': _('Connected to SharePoint site "%s".') % site,
                'type': 'success',
                'sticky': False,
            },
        }
