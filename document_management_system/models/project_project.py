# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from . import sharepoint
from .graph_client import DmsGraphError, get_settings

MANAGER_GROUP = 'document_management_system.group_dms_manager'

# set only by the module's own methods, which run with sudo after their own checks
PROTECTED_FIELDS = {'dms_folder_item_id', 'dms_member_user_ids', 'dms_sync_state', 'dms_last_sync', 'dms_last_error'}
# DMS managers only (who links a code is still an open decision; managers for now)
MANAGER_FIELDS = {'dms_unique_code_id', 'dms_extra_member_ids'}
# project fields the member rule reads
MEMBER_SOURCES = {'user_id', 'privacy_visibility', 'allowed_internal_user_ids', 'dms_extra_member_ids'}


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
    # lets the form make manager-only fields read only for everyone else (the server checks again)
    dms_can_manage = fields.Boolean(compute='_compute_dms_can_manage')

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

    @api.depends_context('uid')
    def _compute_dms_can_manage(self):
        can_manage = self.env.user.has_group(MANAGER_GROUP)
        for project in self:
            project.dms_can_manage = can_manage

    @api.constrains('dms_unique_code_id', 'company_id')
    def _check_dms_unique_code_company(self):
        for project in self.sudo():
            code = project.dms_unique_code_id
            if code and code.company_id != project.company_id:
                raise ValidationError(_('Unique code %(code)s belongs to %(company)s, not to the project\'s company.',
                                        code=code.name, company=code.company_id.name))

    # ------------------------------------------------------------------
    # protection
    # ------------------------------------------------------------------

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

    def _dms_check_code_change(self, code_id):
        for project in self.sudo():
            if project.dms_unique_code_id.id != (code_id or False) and project.dms_file_ids:
                raise UserError(_('Project %s already has files, so its unique code can no longer change.')
                                % project.name)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._dms_check_values(vals, creating=True)
        projects = super().create(vals_list)
        projects.filtered('dms_unique_code_id')._dms_link_code()
        return projects

    def write(self, vals):
        self._dms_check_values(vals)
        if 'dms_unique_code_id' in vals:
            self._dms_check_code_change(vals['dms_unique_code_id'])
        result = super().write(vals)
        if 'dms_unique_code_id' in vals:
            self._dms_link_code()
        elif MEMBER_SOURCES & set(vals):
            self.filtered('dms_unique_code_id')._dms_update_members()
        return result

    # ------------------------------------------------------------------
    # folder and members
    # ------------------------------------------------------------------

    def _dms_link_code(self):
        """a project just got (or lost) its unique code: its folder Projects/<year>/<code>/ and its members"""
        for project in self.sudo():
            if project.dms_unique_code_id:
                project._dms_ensure_folder(force=True)
                project._dms_update_members()
            else:
                project.write({'dms_folder_item_id': False, 'dms_member_user_ids': [(5, 0, 0)]})

    def _dms_folder_path(self):
        """the folder below the library, as odoo names it: Projects/<year>/<code>/"""
        self.ensure_one()
        code = self.sudo().dms_unique_code_id
        year = (code.date or fields.Date.context_today(self)).year
        return 'Projects/%d/%s/' % (year, sharepoint.clean_filename(code.name))

    def _dms_ensure_folder(self, force=False):
        """the id of the project's sharepoint folder, created when missing. no graph call when it is known"""
        self.ensure_one()
        project = self.sudo()
        if project.dms_folder_item_id and not force:
            return project.dms_folder_item_id
        code = project.dms_unique_code_id
        if not code:
            raise UserError(_('Link this project to a unique code first.'))
        settings = get_settings(self.env)
        if not (settings['drive_id'] and settings['projects_folder_id']):
            raise UserError(_('SharePoint is not set up yet. An administrator fills in the document library and the '
                              '"Projects" folder in Settings > Document Management.'))
        year = str((code.date or fields.Date.context_today(self)).year)
        File = self.env['dms.file']
        try:
            client = File._dms_client()
            year_id = sharepoint.ensure_folder(client, settings['drive_id'], settings['projects_folder_id'], year)
            folder_id = sharepoint.ensure_folder(client, settings['drive_id'], year_id,
                                                 sharepoint.clean_filename(code.name))
        except DmsGraphError as e:
            raise UserError(_('The project folder could not be created in SharePoint. %s') % File._dms_error_message(e))
        project.write({'dms_folder_item_id': folder_id})
        return folder_id

    def _dms_compute_member_users(self):
        """who may open this project's files. the one place the member rule lives:
        - the project manager
        - the invited internal users, when the project is visible to invited users only
        - the team and project manager of every active work order under the unique code
        - everyone who requested an active number under the unique code
        - the extra members set by DMS managers
        minus inactive users, portal users and users without a microsoft 365 address"""
        self.ensure_one()
        project = self.sudo()
        users = project.user_id | project.dms_extra_member_ids
        if project.privacy_visibility == 'followers':
            users |= project.allowed_internal_user_ids
        if project.dms_unique_code_id:
            numbers = self.env['doc.number'].sudo().search([
                ('unique_code_id', '=', project.dms_unique_code_id.id), ('state', '=', 'active')])
            work_orders = numbers.filtered(lambda n: n.project_role == 'work_order')
            users |= (work_orders.mapped('team_ids') | work_orders.mapped('pm_id')).mapped('user_id')
            users |= numbers.mapped('requested_by_id')
        return users.filtered(lambda u: u.active and not u.share and u._dms_email())

    def _dms_update_members(self):
        """stores the members; when they changed and files are shared, the project waits for the member sync"""
        changed = False
        for project in self.sudo():
            users = project._dms_compute_member_users()
            if users == project.dms_member_user_ids:
                continue
            vals = {'dms_member_user_ids': [(6, 0, users.ids)]}
            if project.dms_file_ids.filtered(lambda f: f.state == 'shared'):
                vals['dms_sync_state'] = 'pending'
            project.write(vals)
            changed = True
        if changed:
            # record rules are cached per user; the file rule reads the stored members
            self.env['ir.rule'].clear_caches()

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------

    def _dms_check_can_add(self):
        self.ensure_one()
        if not self.dms_unique_code_id:
            raise UserError(_('Link this project to a unique code first. Only projects with a unique code hold files.'))
        self._dms_update_members()
        user = self.env.user
        if not user.has_group(MANAGER_GROUP) and user not in self.sudo().dms_member_user_ids:
            raise AccessError(_('Only members of this project can add files.'))

    def action_dms_add_file(self):
        self.ensure_one()
        self._dms_check_can_add()
        return {
            'name': _('Add File'),
            'type': 'ir.actions.act_window',
            'res_model': 'dms.file.add.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_project_id': self.id},
        }

    def action_dms_open_files(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('document_management_system.action_dms_file')
        action['domain'] = [('project_id', '=', self.id)]
        action['context'] = {'search_default_shared': 1}
        return action
