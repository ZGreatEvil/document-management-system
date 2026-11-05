# -*- coding: utf-8 -*-
import base64
import binascii

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..models import sharepoint


class DmsFileAddWizard(models.TransientModel):
    _name = 'dms.file.add.wizard'
    _description = 'Add Project File'

    project_id = fields.Many2one('project.project', required=True, readonly=True, ondelete='cascade')
    unique_code_id = fields.Many2one(related='project_id.dms_unique_code_id')
    mode = fields.Selection([('upload', 'Upload from this computer'), ('link', 'Paste a SharePoint link')],
                            string='How', default='upload', required=True)
    link = fields.Char(string='SharePoint Link',
                       help='The link of a file in your own upload folder (in SharePoint: the file\'s "Copy link").')
    # attachment=False: the content stays in this transient record only, never in the filestore
    # (not required at database level: the content is cleared once the file is in sharepoint)
    file = fields.Binary(string='File', attachment=False)
    filename = fields.Char()
    number_id = fields.Many2one(
        'doc.number', string='Document Number',
        domain="[('unique_code_id', '=', unique_code_id), ('state', '=', 'active')]",
        help='The number this file belongs to: the file is then named after it. Leave empty for other files.')
    current_file_id = fields.Many2one('dms.file', compute='_compute_current_file')
    current_file_name = fields.Char(compute='_compute_current_file')
    replace_current = fields.Boolean(string='Replace the current file',
                                     help='The current file stays in SharePoint, marked as superseded.')
    final_name = fields.Char(string='Name in SharePoint', compute='_compute_final_name')

    @api.depends('number_id')
    def _compute_current_file(self):
        for wizard in self:
            current = self.env['dms.file']
            if wizard.number_id:
                current = current.sudo().search([('number_id', '=', wizard.number_id.id), ('state', '=', 'shared')],
                                                limit=1).sudo(False)
            wizard.current_file_id = current
            wizard.current_file_name = current.sudo().name or False

    @api.depends('mode', 'filename', 'number_id')
    def _compute_final_name(self):
        for wizard in self:
            if wizard.mode == 'link':
                # the file's own name is only known once the link is resolved
                wizard.final_name = _('%s + the file\'s extension') % sharepoint.number_filename(
                    wizard.number_id.display_number, '') if wizard.number_id else False
            elif not wizard.filename:
                wizard.final_name = False
            elif wizard.number_id:
                wizard.final_name = sharepoint.number_filename(wizard.number_id.display_number, wizard.filename)
            else:
                wizard.final_name = sharepoint.clean_filename(wizard.filename)

    def _check_number(self, number):
        if number.state != 'active':
            raise UserError(_('%s is cancelled and cannot get files.') % number.display_number)
        if number.unique_code_id != self.project_id.dms_unique_code_id:
            raise UserError(_('%s does not belong to this project\'s unique code.') % number.display_number)
        # same rule as changing the number: its division only, numbering administrators excepted
        if not self.env.user.has_group('document_numbering.group_doc_number_manager'):
            number._check_same_division()

    def action_add(self):
        self.ensure_one()
        project = self.project_id
        project._dms_check_can_add()
        data = None
        if self.mode == 'link':
            if not (self.link or '').strip():
                raise UserError(_('Paste the link of the file in your upload folder.'))
        else:
            if not self.file:
                raise UserError(_('Choose a file to add.'))
            try:
                data = base64.b64decode(self.file)
            except (binascii.Error, ValueError):
                raise UserError(_('The file could not be read. Choose it again.'))
        number = self.number_id
        current = self.env['dms.file']
        if number:
            self._check_number(number)
            current = self.env['dms.file'].sudo().search([('number_id', '=', number.id), ('state', '=', 'shared')],
                                                         limit=1)
            if current and not self.replace_current:
                raise UserError(_('%(number)s already has a file (%(name)s). Tick "Replace the current file" to add '
                                  'this one as the new version; the old one is kept as superseded.',
                                  number=number.display_number, name=current.name))
        File = self.env['dms.file']
        if self.mode == 'link':
            File._dms_add_from_link(project, self.link, number=number or None, supersede=current or None)
        else:
            File._dms_add(project, data, self.filename or 'file', number=number or None, supersede=current or None)
            # the content is no longer needed
            self.write({'file': False})
        return {'type': 'ir.actions.act_window_close'}

    def action_upload_folder(self):
        return self.env['res.users'].action_dms_upload_folder()
