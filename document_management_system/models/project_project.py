# -*- coding: utf-8 -*-
import logging
import threading

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import config

from . import sharepoint
from .graph_client import DmsGraphError, get_settings

_logger = logging.getLogger(__name__)

MANAGER_GROUP = 'document_management_system.group_dms_manager'

# set only by the module's own methods, which run with sudo after their own checks
PROTECTED_FIELDS = {'dms_folder_item_id', 'dms_member_user_ids', 'dms_sync_state', 'dms_last_sync', 'dms_last_error'}
# DMS managers only
MANAGER_FIELDS = {'dms_unique_code_id'}
# project fields odoo's project access rules read
MEMBER_SOURCES = {'privacy_visibility', 'allowed_internal_user_ids', 'company_id'}


class Project(models.Model):
    _inherit = 'project.project'

    dms_unique_code_id = fields.Many2one(
        'doc.number.unique.code', string='Unique Code', copy=False, index=True, ondelete='restrict', tracking=True,
        help='The project code whose files this project holds. Its SharePoint folder is Projects/<year>/<code>/.')
    # the code alone (its display name adds customer and project type), for the kanban badge
    dms_unique_code_name = fields.Char(related='dms_unique_code_id.name', string='Unique Code Name')
    dms_folder_item_id = fields.Char(string='SharePoint Folder ID', copy=False, readonly=True, groups=MANAGER_GROUP)
    dms_file_ids = fields.One2many('dms.file', 'project_id', string='Files')
    dms_file_count = fields.Integer(compute='_compute_dms_file_count', string='File Count')
    # a stored copy written by the member sync, so record rules can use it
    dms_member_user_ids = fields.Many2many(
        'res.users', 'dms_project_member_rel', 'project_id', 'user_id', string='Members (files)',
        copy=False, readonly=True)
    dms_sync_state = fields.Selection(
        [('ok', 'Up to date'), ('pending', 'Pending'), ('error', 'Error')],
        string='File Access', default='ok', copy=False, readonly=True)
    dms_last_sync = fields.Datetime(string='Last Sync', copy=False, readonly=True)
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
            raise AccessError(_('Only Document Management administrators can link a unique code.'))

    def _dms_check_code_change(self, code_id):
        if code_id:
            # checked before sharepoint is touched; the unique index stays as the last guard
            taken = self.sudo().with_context(active_test=False).search(
                [('dms_unique_code_id', '=', code_id), ('id', 'not in', self.ids)], limit=1)
            if taken:
                raise UserError(_('This unique code is already linked to project %s.') % taken.name)
        for project in self.sudo():
            if project.dms_unique_code_id.id != (code_id or False) and project.dms_file_ids:
                raise UserError(_('Project %s already has files, so its unique code can no longer change.')
                                % project.name)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._dms_check_values(vals, creating=True)
            if vals.get('dms_unique_code_id'):
                self.browse()._dms_check_code_change(vals['dms_unique_code_id'])
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
        """who may open this project's files. the one place the member rule lives (agreed 2026-11-05):
        exactly the active internal users who can open the project in odoo, as odoo's own project access rules
        decide (invited users of an invited-only project, every employee otherwise, project administrators always,
        each within their companies), minus users without a microsoft 365 address"""
        self.ensure_one()
        members = self.env['res.users']
        for user in self.env['res.users'].sudo().search([('share', '=', False)]):
            if not user._dms_email():
                continue
            project = self.with_user(user).with_context(allowed_company_ids=user.company_ids.ids)
            if project.check_access_rights('read', raise_exception=False) and project._filter_access_rules('read'):
                members |= user
        return members

    def _dms_update_members(self):
        """stores the members; when they changed and files are shared, the project waits for the member sync"""
        changed = False
        for project in self.sudo():
            users = project._dms_compute_member_users()
            # archived users still count as stored: they are exactly the ones whose access must go
            if users == project.with_context(active_test=False).dms_member_user_ids:
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
    # member sync (7.4)
    # ------------------------------------------------------------------

    def _dms_sync_files(self):
        """brings sharepoint in line with odoo: files of cancelled numbers are deleted (agreed 2026-11-05), and access
        to every shared file follows the members. superseded files keep their access (history), removed ones have
        no link. returns True when everything went through"""
        self.ensure_one()
        project = self.sudo()
        File = self.env['dms.file']
        project._dms_update_members()
        wanted = {user.id: user._dms_email() for user in project.dms_member_user_ids}
        try:
            client = File._dms_client()
            for file in project._dms_cancelled_number_files():
                file._dms_delete_for_cancelled_number(client)
            for file in project.dms_file_ids.filtered(lambda f: f.state == 'shared'):
                file._dms_sync_grants(client, wanted)
        except DmsGraphError as e:
            project.write({'dms_sync_state': 'error', 'dms_last_error': File._dms_error_message(e),
                           'dms_last_sync': fields.Datetime.now()})
            return False
        project.write({'dms_sync_state': 'ok', 'dms_last_error': False, 'dms_last_sync': fields.Datetime.now()})
        return True

    def _dms_cancelled_number_files(self):
        return self.sudo().dms_file_ids.filtered(lambda f: f.state != 'removed' and f.number_id.state == 'cancelled')

    @api.model
    def _dms_commit(self):
        """crons keep each project's progress; tests run in one transaction that must not be committed"""
        if not (getattr(threading.current_thread(), 'testing', False) or config['test_enable']):
            self.env.cr.commit()

    @api.model
    def _dms_cron_sync(self, limit=100):
        """every 5 minutes: projects waiting for the member sync, or whose last sync failed"""
        projects = self.sudo().search([('dms_unique_code_id', '!=', False),
                                       ('dms_sync_state', 'in', ('pending', 'error'))], limit=limit, order='id')
        for project in projects:
            project._dms_sync_files()
            self._dms_commit()

    @api.model
    def _dms_cron_drift(self):
        """nightly: compares every shared file in sharepoint with odoo. links odoo did not make are deleted, a lost
        odoo link is made again, a file gone from sharepoint is marked as an error; each finding goes in the
        project's chatter"""
        File = self.env['dms.file'].sudo()
        try:
            client = File._dms_client()
        except DmsGraphError as e:
            _logger.warning('dms drift check skipped: %s', e)
            return
        for project in self.sudo().search([('dms_unique_code_id', '!=', False)], order='id'):
            notes = []
            wanted = {user.id: user._dms_email() for user in project.dms_member_user_ids}
            for file in project.dms_file_ids.filtered(lambda f: f.state == 'shared'):
                try:
                    perms = sharepoint.list_permissions(client, file.drive_id, file.item_id)
                    foreign = [p for p in perms if p.get('link') and p['link'].get('scope') != 'existingAccess'
                               and p['id'] != file.link_permission_id]
                    for perm in foreign:
                        client.delete('/drives/%s/items/%s/permissions/%s' % (file.drive_id, file.item_id, perm['id']))
                    if foreign:
                        notes.append(_('%(file)s: %(count)s link(s) not made by Odoo were deleted.',
                                       file=file.name, count=len(foreign)))
                    if not any(p['id'] == file.link_permission_id for p in perms):
                        file._dms_relink(client, wanted)
                        notes.append(_('%s: its Odoo link was missing and has been made again.') % file.name)
                except DmsGraphError as e:
                    if e.status != 404:
                        _logger.warning('dms drift check of a file failed: HTTP %s %s', e.status, e.code)
                        continue
                    file.write({'state': 'error', 'last_error': _('The file is no longer in SharePoint.')})
                    notes.append(_('%s is no longer in SharePoint.') % file.name)
            if notes:
                project.message_post(body='<br/>'.join([_('File access check:')] + notes))
            self._dms_commit()

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------

    def action_dms_sync_now(self):
        self.ensure_one()
        if not self.env.user.has_group(MANAGER_GROUP):
            raise AccessError(_('Only Document Management administrators can start the file access sync.'))
        if not self.dms_unique_code_id:
            raise UserError(_('This project has no unique code, so it has no files to sync.'))
        if not self._dms_sync_files():
            raise UserError(_('The sync did not finish: %s It is tried again automatically every 5 minutes.')
                            % self.sudo().dms_last_error)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {'title': _('File access is up to date'), 'type': 'success', 'sticky': False,
                       'message': _('Every shared file of %s now matches the member list.') % self.name},
        }

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

    def action_dms_upload_folder(self):
        return self.env['res.users'].action_dms_upload_folder()

    def action_dms_open_files(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('document_management_system.action_dms_file')
        action['domain'] = [('project_id', '=', self.id)]
        action['context'] = {'search_default_shared': 1}
        return action
