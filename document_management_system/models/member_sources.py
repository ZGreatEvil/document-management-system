# -*- coding: utf-8 -*-
"""changes elsewhere that the file access must follow. members are worked out again in the database only; projects
with shared files are then marked pending, and the member sync cron does the sharepoint part."""
from odoo import api, models

# res.users fields that decide whether a user can open a project and with which address they get access
USER_FIELDS = {'active', 'login', 'dms_m365_email', 'groups_id', 'company_ids', 'share'}
ADDRESS_FIELDS = {'login', 'dms_m365_email'}
# res.groups fields that change who is in a group (e.g. becoming a project administrator)
GROUP_FIELDS = {'users', 'implied_ids'}


def _all_linked_projects(env):
    return env['project.project'].sudo().search([('dms_unique_code_id', '!=', False)])


class DocNumber(models.Model):
    _inherit = 'doc.number'

    def write(self, vals):
        # _cancel() writes the state through here as well
        result = super().write(vals)
        if vals.get('state') == 'cancelled':
            files = self.env['dms.file'].sudo().search([('number_id', 'in', self.ids), ('state', '!=', 'removed')])
            # the member sync deletes them in sharepoint, so cancelling never waits for (or fails on) sharepoint
            files.mapped('project_id').write({'dms_sync_state': 'pending'})
        return result


class ResUsers(models.Model):
    _inherit = 'res.users'

    @api.model_create_multi
    def create(self, vals_list):
        users = super().create(vals_list)
        if users.filtered(lambda u: not u.share):
            _all_linked_projects(self.env)._dms_update_members()
        return users

    def write(self, vals):
        result = super().write(vals)
        if USER_FIELDS & set(vals):
            Project = self.env['project.project'].sudo()
            # being reactivated or given more rights can open any project: rare, so all are checked
            _all_linked_projects(self.env)._dms_update_members()
            if ADDRESS_FIELDS & set(vals):
                # same members, other address: their files must be granted to the new one
                projects = Project.search([('dms_member_user_ids', 'in', self.ids)])
                projects.filtered(lambda p: p.dms_file_ids.filtered(lambda f: f.state == 'shared')).write(
                    {'dms_sync_state': 'pending'})
        return result


class ResGroups(models.Model):
    _inherit = 'res.groups'

    def write(self, vals):
        result = super().write(vals)
        if GROUP_FIELDS & set(vals):
            _all_linked_projects(self.env)._dms_update_members()
        return result
