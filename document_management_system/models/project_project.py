# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

MANAGER_GROUP = 'document_management_system.group_dms_manager'

# set only by the module's own methods, which run with sudo after their own checks
PROTECTED_FIELDS = {'dms_folder_item_id', 'dms_member_user_ids', 'dms_sync_state', 'dms_last_sync', 'dms_last_error'}
# DMS managers only (who links a code is still an open decision; managers for now)
MANAGER_FIELDS = {'dms_unique_code_id', 'dms_extra_member_ids'}


class Project(models.Model):
    _inherit = 'project.project'

    dms_unique_code_id = fields.Many2one(
        'doc.number.unique.code', string='Unique Code', copy=False, index=True, ondelete='restrict', tracking=True,
        help='The project code whose files this project holds. Its SharePoint folder is Projects/<year>/<code>/.')
    dms_folder_item_id = fields.Char(string='SharePoint Folder ID', copy=False, readonly=True, groups=MANAGER_GROUP)
    dms_file_ids = fields.One2many('dms.file', 'project_id', string='Files')
    dms_file_count = fields.Integer(compute='_compute_dms_file_count', string='File Count')
    # a stored copy written by the member sync, so record rules can use it
    dms_member_user_ids = fields.Many2many(
        'res.users', 'dms_project_member_rel', 'project_id', 'user_id', string='Members (files)',
        copy=False, readonly=True)
    dms_extra_member_ids = fields.Many2many(
        'res.users', 'dms_project_extra_member_rel', 'project_id', 'user_id', string='Extra Members (files)',
        copy=False, domain=[('share', '=', False)])
    dms_sync_state = fields.Selection(
        [('ok', 'Up to date'), ('pending', 'Pending'), ('error', 'Error')],
        string='File Access', default='ok', copy=False, readonly=True)
    dms_last_sync = fields.Datetime(string='Last File Access Sync', copy=False, readonly=True)
    dms_last_error = fields.Text(string='Last Sync Error', copy=False, readonly=True, groups=MANAGER_GROUP)

    _sql_constraints = [
        ('dms_unique_code_uniq', 'unique(dms_unique_code_id)', 'This unique code is already linked to another project.'),
    ]

    @api.depends_context('uid')
    def _compute_dms_file_count(self):
        # counted as the user, so files they cannot see are not counted
        data = self.env['dms.file'].read_group([('project_id', 'in', self.ids)], ['project_id'], ['project_id'])
        counts = {d['project_id'][0]: d['project_id_count'] for d in data}
        for project in self:
            project.dms_file_count = counts.get(project.id, 0)

    @api.constrains('dms_unique_code_id', 'company_id')
    def _check_dms_unique_code_company(self):
        for project in self.sudo():
            code = project.dms_unique_code_id
            if code and code.company_id != project.company_id:
                raise ValidationError(_('Unique code %(code)s belongs to %(company)s, not to the project\'s company.',
                                        code=code.name, company=code.company_id.name))

    @api.model
    def _dms_check_values(self, vals, creating=False):
        if self.env.su:
            return
        # a new project may carry empty values for these; that changes nothing
        keys = {key for key, value in vals.items() if value} if creating else set(vals)
        if PROTECTED_FIELDS & keys:
            raise AccessError(_('Project file access is kept up to date by the system and cannot be changed by hand.'))
        if MANAGER_FIELDS & keys and not self.env.user.has_group(MANAGER_GROUP):
            raise AccessError(_('Only Document Management administrators can link a unique code '
                                'or add extra file members.'))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._dms_check_values(vals, creating=True)
        return super().create(vals_list)

    def write(self, vals):
        self._dms_check_values(vals)
        return super().write(vals)
