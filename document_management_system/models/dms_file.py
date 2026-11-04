# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

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
    original_filename = fields.Char(readonly=True)
    source = fields.Selection([('upload', 'Uploaded in Odoo'), ('link', 'Moved from an upload folder')],
                              required=True, readonly=True)
    drive_id = fields.Char(string='Drive ID', readonly=True, groups=MANAGER_GROUP)
    item_id = fields.Char(string='Item ID', readonly=True, index=True, groups=MANAGER_GROUP)
    link_permission_id = fields.Char(string='Link Permission ID', readonly=True, groups=MANAGER_GROUP)
    share_url = fields.Char(string='Link', readonly=True,
                            help='The specific-people link. SharePoint refuses anyone who is not a project member.')
    # bytes; a float column so files above 2 GiB (moved from an upload folder) still fit
    size = fields.Float(digits=(20, 0), readonly=True)
    mimetype = fields.Char(readonly=True)
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
