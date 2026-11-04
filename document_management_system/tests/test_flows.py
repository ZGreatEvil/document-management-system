# -*- coding: utf-8 -*-
import base64
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tests.common import new_test_user

from odoo.addons.document_management_system.models import sharepoint
from odoo.addons.document_management_system.models.graph_client import DmsGraphError

from .test_models import DmsCommon


@tagged('post_install', '-at_install', 'dms')
class TestNaming(DmsCommon):

    def test_number_name_from_the_brief(self):
        self.assertEqual(sharepoint.number_filename('QTN-001/SDX001/XI/26 Rev.1', 'scan final.pdf'),
                         'QTN-001_SDX001_XI_26_Rev.1.pdf')
        self.assertEqual(sharepoint.number_filename('QTN-001/SDX001/XI/26', 'no extension'), 'QTN-001_SDX001_XI_26')

    def test_forbidden_characters_replaced(self):
        self.assertEqual(sharepoint.clean_filename('a"b*c:d<e>f?g/h\\i|j#k%l.pdf'), 'a_b_c_d_e_f_g_h_i_j_k_l.pdf')
        self.assertEqual(sharepoint.clean_filename('  spaced name .docx  '), 'spaced name.docx')
        self.assertEqual(sharepoint.clean_filename('tab\there.txt'), 'tab_here.txt')

    def test_reserved_names(self):
        self.assertEqual(sharepoint.clean_filename('CON.txt'), '_CON.txt')
        self.assertEqual(sharepoint.clean_filename('lpt9'), '_lpt9')
        self.assertEqual(sharepoint.clean_filename('desktop.ini'), '_desktop.ini')
        self.assertEqual(sharepoint.clean_filename('.lock'), '_.lock')
        self.assertEqual(sharepoint.clean_filename('~$draft.docx'), '_draft.docx')
        self.assertEqual(sharepoint.clean_filename('site_vti_cnf.txt'), 'site_vti-cnf.txt')
        self.assertEqual(sharepoint.clean_filename('console.txt'), 'console.txt')

    def test_empty_names(self):
        self.assertEqual(sharepoint.clean_filename(''), 'file')
        self.assertEqual(sharepoint.clean_filename('   '), 'file')
        self.assertEqual(sharepoint.clean_filename('???'), '___')

    def test_length_limit(self):
        folder = 'Projects/2026/ZDMQB901/'
        long_name = 'x' * 500 + '.pdf'
        fitted = sharepoint.fit_length(long_name, folder)
        self.assertTrue(fitted.endswith('.pdf'))
        self.assertLessEqual(len(folder) + len(fitted) + sharepoint.SITE_PATH_MARGIN, sharepoint.MAX_PATH)
        self.assertEqual(sharepoint.fit_length('short.pdf', folder), 'short.pdf')


@tagged('post_install', '-at_install', 'dms')
class TestMembers(DmsCommon):

    def make_user(self, login, groups='base.group_user', email=True):
        user = new_test_user(self.env, login=login, groups=groups, company_id=self.company.id)
        if email:
            user.dms_m365_email = '%s@example.test' % login
        return user

    def test_member_rule(self):
        pm = self.make_user('dms_rule_pm')
        invited = self.make_user('dms_rule_invited')
        requester = self.make_user('dms_rule_requester')
        cancelled_requester = self.make_user('dms_rule_cancelled')
        technician = self.make_user('dms_rule_tech')
        no_address = self.make_user('dms_rule_noaddress', email=False)
        archived = self.make_user('dms_rule_archived')
        tech_employee = self.env['hr.employee'].create({'name': 'Tech', 'user_id': technician.id,
                                                        'department_id': self.dept_tec.id})
        project = self.project
        project.write({'user_id': pm.id, 'privacy_visibility': 'followers',
                       'allowed_internal_user_ids': [(6, 0, (invited | no_address | archived).ids)]})
        self.make_number('BST-801/ZDMQB901/XI/26', self.code, self.type_bst, self.div_tec, requester)
        self.make_number('BST-802/ZDMQB901/XI/26', self.code, self.type_bst, self.div_tec, cancelled_requester,
                         state='cancelled')
        self.make_number('WO-801/ZDMQB901/XI/26', self.code, self.type_wo, self.div_tec,
                         team_ids=[(6, 0, (tech_employee | self.emp_member).ids)], pm_id=tech_employee.id)
        archived.active = False

        members = project._dms_compute_member_users()
        self.assertEqual(members, pm | invited | requester | technician | self.member)
        for left_out in (no_address, archived, cancelled_requester, self.outsider):
            self.assertNotIn(left_out, members)

    def test_invited_users_count_only_for_invited_projects(self):
        invited = self.make_user('dms_rule_invited2')
        self.project.write({'privacy_visibility': 'employees', 'allowed_internal_user_ids': [(6, 0, invited.ids)]})
        self.assertNotIn(invited, self.project._dms_compute_member_users())
        self.project.write({'privacy_visibility': 'followers'})
        self.assertIn(invited, self.project._dms_compute_member_users())

    def test_portal_users_never_members(self):
        portal = self.make_user('dms_rule_portal', groups='base.group_portal')
        self.project.sudo().write({'dms_extra_member_ids': [(4, portal.id)]})
        self.assertNotIn(portal, self.project.dms_member_user_ids)

    def test_member_change_stored_and_marks_shared_files_pending(self):
        newcomer = self.make_user('dms_rule_newcomer')
        self.project.write({'dms_extra_member_ids': [(4, newcomer.id)]})
        self.assertIn(newcomer, self.project.dms_member_user_ids)
        self.assertEqual(self.project.dms_sync_state, 'ok')  # no files yet
        self.make_file()
        self.project.write({'dms_extra_member_ids': [(3, newcomer.id)]})
        self.assertNotIn(newcomer, self.project.dms_member_user_ids)
        self.assertEqual(self.project.dms_sync_state, 'pending')


@tagged('post_install', '-at_install', 'dms')
class TestLinkProject(DmsCommon):

    def new_project(self):
        return self.env['project.project'].with_user(self.manager).create(
            {'name': 'DMS link test', 'company_id': self.company.id, 'user_id': self.manager.id})

    def test_link_creates_year_and_code_folders(self):
        project = self.new_project()
        project.write({'dms_unique_code_id': self.code_other.id})
        folder_id = project.sudo().dms_folder_item_id
        year = str(self.code_other.date.year)
        self.assertEqual(self.graph.path_of(folder_id), '%s/ZDMQB902' % year)
        self.assertEqual(self.graph.items[folder_id]['parent'],
                         next(i['id'] for i in self.graph.items.values() if i['name'] == year))
        self.assertIn(self.manager, project.dms_member_user_ids)
        self.assertEqual(project._dms_folder_path(), 'Projects/%s/ZDMQB902/' % year)

    def test_existing_folders_are_reused(self):
        # the year folder exists already (made by the setup project): 409, then looked up
        project = self.new_project()
        project.write({'dms_unique_code_id': self.code_other.id})
        methods = [call[0] for call in self.graph.calls]
        self.assertEqual(methods, ['POST', 'GET', 'POST'])

    def test_graph_failure_saves_nothing(self):
        project = self.new_project()
        self.graph.fail[('POST', '/children')] = DmsGraphError(403, 'accessDenied')
        with self.assertRaisesRegex(UserError, 'could not be created'):
            with self.env.cr.savepoint():
                project.write({'dms_unique_code_id': self.code_other.id})
        project.invalidate_cache()
        self.assertFalse(project.dms_unique_code_id)

    def test_missing_settings_explained(self):
        self.env['ir.config_parameter'].sudo().set_param('document_management_system.drive_id', '')
        project = self.new_project()
        with self.assertRaisesRegex(UserError, 'not set up'):
            project.write({'dms_unique_code_id': self.code_other.id})

    def test_code_cannot_change_once_files_exist(self):
        self.make_file()
        with self.assertRaisesRegex(UserError, 'already has files'):
            self.project.with_user(self.manager).write({'dms_unique_code_id': self.code_other.id})

    def test_unlinking_clears_folder_and_members(self):
        project = self.new_project()
        project.write({'dms_unique_code_id': self.code_other.id})
        project.write({'dms_unique_code_id': False})
        self.assertFalse(project.sudo().dms_folder_item_id)
        self.assertFalse(project.dms_member_user_ids)

    def test_no_graph_call_without_a_code(self):
        self.new_project().write({'name': 'renamed'})
        self.assertEqual(self.graph.calls, [])


@tagged('post_install', '-at_install', 'dms')
class TestUpload(DmsCommon):

    def wizard(self, user=None, content=b'%PDF-1.4 test', filename='Report final.pdf', **values):
        vals = {'project_id': self.project.id, 'file': base64.b64encode(content), 'filename': filename}
        vals.update(values)
        return self.env['dms.file.add.wizard'].with_user(user or self.member).create(vals)

    def files(self):
        return self.env['dms.file'].search([('project_id', '=', self.project.id)])

    def test_member_uploads_without_number(self):
        self.wizard().action_add()
        file = self.files()
        self.assertEqual(len(file), 1)
        self.assertEqual(file.name, 'Report final.pdf')
        self.assertEqual(file.source, 'upload')
        self.assertEqual(file.uploaded_by_id, self.member)
        self.assertEqual(file.state, 'shared')
        self.assertEqual(file.mimetype, 'application/pdf')
        item_id = file.sudo().item_id
        self.assertEqual(self.graph.items[item_id]['parent'], self.project.sudo().dms_folder_item_id)
        # the url grant answered with, not the one createLink gave
        self.assertEqual(file.share_url, 'https://sharepoint.test/link/%s?granted' % item_id)
        self.assertEqual(self.graph.emails_on_link(item_id), {'dms_member@example.test'})
        self.assertEqual(file.sudo().grant_ids.mapped('user_id'), self.member)
        self.assertEqual(file.sudo().link_permission_id, 'perm-%s' % item_id)
        put = next(path for method, path in self.graph.calls if method == 'PUT')
        self.assertIn('conflictBehavior=rename', put)

    def test_same_name_twice_is_renamed(self):
        self.wizard().action_add()
        self.wizard().action_add()
        self.assertEqual(sorted(self.files().mapped('name')), ['Report final 1.pdf', 'Report final.pdf'])

    def test_wizard_content_cleared(self):
        wizard = self.wizard()
        wizard.action_add()
        self.assertFalse(wizard.file)

    def test_numbered_file_named_after_the_number(self):
        number = self.make_number('BST-901/ZDMQB901/XI/26', self.code, self.type_bst, self.div_tec)
        wizard = self.wizard(number_id=number.id)
        self.assertEqual(wizard.final_name, 'BST-901_ZDMQB901_XI_26.pdf')
        wizard.action_add()
        file = self.files()
        self.assertEqual(file.name, 'BST-901_ZDMQB901_XI_26.pdf')
        self.assertEqual(file.number_id, number)
        put = next(path for method, path in self.graph.calls if method == 'PUT')
        self.assertIn('conflictBehavior=fail', put)

    def test_numbered_name_taken_explained_and_nothing_saved(self):
        number = self.make_number('BST-902/ZDMQB901/XI/26', self.code, self.type_bst, self.div_tec)
        folder = self.project.sudo().dms_folder_item_id
        self.graph.items['manual'] = {'id': 'manual', 'name': 'BST-902_ZDMQB901_XI_26.pdf', 'parent': folder}
        with self.assertRaisesRegex(UserError, 'add a revision'):
            self.wizard(number_id=number.id).action_add()
        self.assertFalse(self.files())

    def test_revision_gives_a_new_name(self):
        number = self.make_number('BST-903/ZDMQB901/XI/26', self.code, self.type_bst, self.div_tec)
        self.wizard(number_id=number.id).action_add()
        number.sudo().write({'revision': 1})
        self.wizard(number_id=number.id, replace_current=True).action_add()
        self.assertEqual(sorted(self.files().mapped('name')),
                         ['BST-903_ZDMQB901_XI_26.pdf', 'BST-903_ZDMQB901_XI_26_Rev.1.pdf'])

    def test_replacing_needs_confirmation_and_supersedes(self):
        number = self.make_number('BST-904/ZDMQB901/XI/26', self.code, self.type_bst, self.div_tec)
        self.wizard(number_id=number.id).action_add()
        first = self.files()
        number.sudo().write({'revision': 1})
        wizard = self.wizard(number_id=number.id)
        self.assertEqual(wizard.current_file_id, first)
        with self.assertRaisesRegex(UserError, 'Replace the current file'):
            wizard.action_add()
        wizard.replace_current = True
        wizard.action_add()
        self.assertEqual(first.state, 'superseded')
        self.assertEqual(self.files().filtered(lambda f: f.state == 'shared').name, 'BST-904_ZDMQB901_XI_26_Rev.1.pdf')

    def test_cancelled_number_refused(self):
        number = self.make_number('BST-905/ZDMQB901/XI/26', self.code, self.type_bst, self.div_tec, state='cancelled')
        with self.assertRaisesRegex(UserError, 'cancelled'):
            self.wizard(number_id=number.id).action_add()

    def test_number_of_another_code_refused(self):
        number = self.make_number('BST-906/ZDMQB902/XI/26', self.code_other, self.type_bst, self.div_tec)
        with self.assertRaisesRegex(UserError, 'does not belong'):
            self.wizard(number_id=number.id).action_add()

    def test_number_of_another_division_refused(self):
        number = self.make_number('SRT-907/ZDMQB901/XI/26', self.code, self.type_srt, self.div_fna)
        with self.assertRaises(AccessError):
            self.wizard(number_id=number.id).action_add()
        self.assertFalse(self.graph.calls)

    def test_numbering_administrator_bypasses_division(self):
        self.member.groups_id |= self.env.ref('document_numbering.group_doc_number_manager')
        number = self.make_number('SRT-908/ZDMQB901/XI/26', self.code, self.type_srt, self.div_fna)
        self.wizard(number_id=number.id).action_add()
        self.assertEqual(self.files().number_id, number)

    def test_non_member_refused(self):
        with self.assertRaisesRegex(AccessError, 'Only members'):
            self.project.with_user(self.outsider).action_dms_add_file()
        with self.assertRaisesRegex(AccessError, 'Only members'):
            self.wizard(user=self.outsider).action_add()
        self.assertFalse(self.graph.calls)

    def test_manager_may_add_without_being_a_member(self):
        self.assertNotIn(self.manager, self.project.dms_member_user_ids)
        self.wizard(user=self.manager).action_add()
        file = self.files()
        # access goes to the members only, not to the manager who uploaded
        self.assertEqual(self.graph.emails_on_link(file.sudo().item_id), {'dms_member@example.test'})

    def test_project_without_code_refused(self):
        project = self.env['project.project'].create({'name': 'No code', 'company_id': self.company.id})
        with self.assertRaisesRegex(UserError, 'unique code first'):
            project.with_user(self.manager).action_dms_add_file()

    def test_empty_or_missing_file_refused(self):
        # an empty file arrives as an empty value, like no file at all
        with self.assertRaisesRegex(UserError, 'Choose a file'):
            self.wizard(content=b'').action_add()
        with self.assertRaisesRegex(UserError, 'Choose a file'):
            self.wizard(file=False).action_add()
        self.assertFalse(self.graph.calls)

    def test_large_file_uses_an_upload_session(self):
        content = b'x' * (sharepoint.SIMPLE_UPLOAD_LIMIT + 1)
        self.wizard(content=content, filename='big.bin').action_add()
        file = self.files()
        self.assertEqual(file.size, len(content))
        chunk_puts = [path for method, path in self.graph.calls if method == 'PUT']
        self.assertEqual(len(chunk_puts), 3)  # 5 MiB, 5 MiB, the rest
        self.assertTrue(all(path.startswith('https://upload.sharepoint.test/') for path in chunk_puts))

    def test_failed_session_is_cancelled(self):
        content = b'x' * (sharepoint.SIMPLE_UPLOAD_LIMIT + 1)
        with patch.object(type(self.graph), 'upload_chunk', side_effect=DmsGraphError(503, 'serviceNotAvailable')):
            with self.assertRaisesRegex(UserError, 'error'):
                self.wizard(content=content, filename='big.bin').action_add()
        cancelled = [path for method, path in self.graph.calls if method == 'DELETE']
        self.assertEqual(len(cancelled), 1)
        self.assertTrue(cancelled[0].startswith('https://upload.sharepoint.test/'))
        self.assertFalse(self.files())

    def test_failed_share_removes_the_upload(self):
        self.graph.fail[('POST', '/permission/grant')] = DmsGraphError(403, 'accessDenied')
        with self.assertRaisesRegex(UserError, 'refused access'):
            self.wizard().action_add()
        deleted = [path for method, path in self.graph.calls if method == 'DELETE']
        self.assertEqual(len(deleted), 1)
        self.assertFalse([i for i in self.graph.items.values() if i['name'] == 'Report final.pdf'])
        self.assertFalse(self.files())

    def test_unreachable_sharepoint_explained(self):
        self.graph.fail[('PUT', '/content')] = DmsGraphError(0, 'networkError')
        with self.assertRaisesRegex(UserError, 'could not be reached'):
            self.wizard().action_add()
        self.assertFalse(self.files())

    def test_non_member_cannot_see_the_file(self):
        self.wizard().action_add()
        file = self.files()
        self.assertFalse(self.env['dms.file'].with_user(self.outsider).search([('id', '=', file.id)]))
        self.assertEqual(self.project.with_user(self.outsider).dms_file_count, 0)
        self.assertEqual(self.project.with_user(self.member).dms_file_count, 1)

    def test_open_returns_the_share_link(self):
        self.wizard().action_add()
        file = self.files().with_user(self.member)
        action = file.action_open()
        self.assertEqual((action['type'], action['target'], action['url']),
                         ('ir.actions.act_url', 'new', file.share_url))
        file.sudo().write({'state': 'removed'})
        with self.assertRaises(UserError):
            file.action_open()
