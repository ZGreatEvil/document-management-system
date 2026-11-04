# -*- coding: utf-8 -*-
from unittest.mock import patch

from psycopg2 import IntegrityError

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import SavepointCase, new_test_user
from odoo.tools import mute_logger

from odoo.addons.document_management_system.models.graph_client import PARAM_PREFIX

from .common import FakeGraph


class DmsCommon(SavepointCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        # graph is never called for real: every client is the in-memory fake
        cls.graph = FakeGraph()
        cls._graph_patch = patch.object(type(env['dms.file']), '_dms_client', lambda self: cls.graph)
        cls._graph_patch.start()
        params = env['ir.config_parameter'].sudo()
        params.set_param(PARAM_PREFIX + 'drive_id', 'drive-1')
        params.set_param(PARAM_PREFIX + 'projects_folder_id', 'projects-root')
        # own companies, codes and numbers, so the tests also pass on a database holding real data
        cls.company = env.company
        cls.company_b = env['res.company'].create({'name': 'DMS Test Company B'})
        companies = cls.company | cls.company_b

        def make_user(login, groups):
            return new_test_user(env, login=login, groups=groups, company_id=cls.company.id,
                                 company_ids=[(6, 0, companies.ids)])

        cls.member = make_user('dms_member', 'base.group_user')
        cls.outsider = make_user('dms_outsider', 'base.group_user')
        cls.project_manager = make_user('dms_pm', 'base.group_user,project.group_project_manager')
        cls.manager = make_user('dms_manager', 'base.group_user,project.group_project_manager,'
                                               'document_management_system.group_dms_manager')
        for user in (cls.member, cls.outsider, cls.project_manager, cls.manager):
            user.dms_m365_email = '%s@example.test' % user.login

        # the member works in the technical division, which numbers BSTs
        cls.dept_tec = env['hr.department'].create({'name': 'DMS Technical (test)', 'company_id': cls.company.id})
        cls.dept_fna = env['hr.department'].create({'name': 'DMS Finance (test)', 'company_id': cls.company.id})
        cls.div_tec = env.ref('document_numbering.division_tec')
        cls.div_fna = env.ref('document_numbering.division_fna')
        cls.div_tec.department_ids = cls.dept_tec
        cls.div_fna.department_ids = cls.dept_fna
        cls.emp_member = env['hr.employee'].create({'name': 'DMS member', 'user_id': cls.member.id,
                                                    'department_id': cls.dept_tec.id, 'company_id': cls.company.id})
        cls.type_bst = env.ref('document_numbering.type_bst')
        cls.type_srt = env.ref('document_numbering.type_srt')
        cls.type_wo = env.ref('document_numbering.type_wo')

        project_type = env['doc.number.project'].create({
            'name': 'DMS test type', 'code': env['doc.number.project']._default_code() + 100,
        })
        partner = env['res.partner'].create({'name': 'DMS Test Customer', 'is_company': True})

        def make_code(name, company):
            return env['doc.number.unique.code'].create({
                'name': name, 'origin': 'import', 'company_id': company.id, 'partner_id': partner.id,
                'client_id': partner.id, 'project_id': project_type.id, 'detail': 'DMS test',
            })

        cls.code = make_code('ZDMQB901', cls.company)
        cls.code_other = make_code('ZDMQB902', cls.company)
        cls.code_b = make_code('ZDMQB903', cls.company_b)

        cls.project = env['project.project'].create({'name': 'DMS test project', 'company_id': cls.company.id,
                                                     'dms_unique_code_id': cls.code.id})
        cls.project.dms_extra_member_ids = cls.member

        cls.type_qtn = env.ref('document_numbering.type_qtn')

    @classmethod
    def tearDownClass(cls):
        cls._graph_patch.stop()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.graph.reset_calls()
        self.addCleanup(self.graph.restore, self.graph.snapshot())

    @classmethod
    def make_number(cls, name, code, doc_type=None, division=None, requested_by=None, **values):
        vals = {
            'origin': 'import', 'name': name, 'type_id': (doc_type or cls.type_qtn).id,
            'company_id': code.company_id.id, 'unique_code_id': code.id,
            'division_id': division.id if division else False,
            'requested_by_id': requested_by.id if requested_by else False,
            'beneficiary': 'DMS test', 'purpose': 'DMS test',
        }
        vals.update(values)
        return cls.env['doc.number'].create(vals)

    @classmethod
    def make_file(cls, name='plan.pdf', project=None, **values):
        vals = {'name': name, 'project_id': (project or cls.project).id, 'source': 'upload',
                'drive_id': 'drive', 'item_id': 'item-%s' % name, 'share_url': 'https://example.test/%s' % name}
        vals.update(values)
        return cls.env['dms.file'].sudo().create(vals)


@tagged('post_install', '-at_install', 'dms')
class TestGroups(DmsCommon):

    def test_internal_users_are_dms_users(self):
        for user in (self.member, self.outsider, self.project_manager):
            self.assertTrue(user.has_group('document_management_system.group_dms_user'))
            self.assertFalse(user.has_group('document_management_system.group_dms_manager'))
        self.assertTrue(self.manager.has_group('document_management_system.group_dms_user'))

    def test_admin_is_manager(self):
        self.assertTrue(self.env.ref('base.user_admin').has_group('document_management_system.group_dms_manager'))


@tagged('post_install', '-at_install', 'dms')
class TestProjectLink(DmsCommon):

    def new_project(self, user=None, **values):
        vals = {'name': 'DMS other project', 'company_id': self.company.id}
        vals.update(values)
        return self.env['project.project'].with_user(user or self.manager).create(vals)

    def test_manager_links_a_code(self):
        project = self.new_project()
        project.write({'dms_unique_code_id': self.code_other.id})
        self.assertEqual(project.dms_unique_code_id, self.code_other)

    def test_project_manager_cannot_link_a_code(self):
        project = self.new_project(user=self.project_manager)
        with self.assertRaisesRegex(AccessError, 'Document Management administrators'):
            project.write({'dms_unique_code_id': self.code_other.id})
        with self.assertRaisesRegex(AccessError, 'Document Management administrators'):
            self.new_project(user=self.project_manager, dms_unique_code_id=self.code_other.id)
        with self.assertRaisesRegex(AccessError, 'Document Management administrators'):
            self.project.with_user(self.project_manager).write({'dms_unique_code_id': False})

    def test_project_manager_sets_no_extra_members(self):
        with self.assertRaises(AccessError):
            self.project.with_user(self.project_manager).write({'dms_extra_member_ids': [(4, self.outsider.id)]})
        self.project.with_user(self.manager).write({'dms_extra_member_ids': [(4, self.outsider.id)]})
        self.assertIn(self.outsider, self.project.dms_extra_member_ids)

    def test_empty_values_on_create_allowed(self):
        project = self.new_project(user=self.project_manager, dms_unique_code_id=False, dms_extra_member_ids=[])
        self.assertFalse(project.dms_unique_code_id)
        self.assertEqual(project.dms_sync_state, 'ok')

    def test_ordinary_project_edits_unaffected(self):
        self.project.with_user(self.project_manager).write({'name': 'Renamed'})
        self.assertEqual(self.project.name, 'Renamed')

    def test_one_project_per_code(self):
        project = self.new_project()
        with mute_logger('odoo.sql_db'), self.assertRaises(IntegrityError):
            with self.env.cr.savepoint():
                project.write({'dms_unique_code_id': self.code.id})
                project.flush()

    def test_code_of_another_company_refused(self):
        with self.assertRaises(ValidationError):
            self.new_project(dms_unique_code_id=self.code_b.id)

    def test_protected_fields_need_the_system(self):
        for vals in ({'dms_member_user_ids': [(4, self.outsider.id)]}, {'dms_sync_state': 'error'},
                     {'dms_folder_item_id': 'x'}, {'dms_last_error': 'x'}, {'dms_last_sync': '2026-01-01 00:00:00'}):
            with self.assertRaises(AccessError, msg=str(vals)):
                self.project.with_user(self.manager).write(vals)
        self.project.sudo().write({'dms_sync_state': 'pending', 'dms_folder_item_id': 'folder-1'})
        self.assertEqual(self.project.dms_sync_state, 'pending')

    def test_copy_keeps_no_dms_data(self):
        copy = self.project.copy()
        self.assertFalse(copy.dms_unique_code_id)
        self.assertFalse(copy.dms_member_user_ids)


@tagged('post_install', '-at_install', 'dms')
class TestFiles(DmsCommon):

    def test_member_sees_files_others_do_not(self):
        file = self.make_file()
        File = self.env['dms.file']
        self.assertEqual(File.with_user(self.member).search([('project_id', '=', self.project.id)]), file)
        self.assertFalse(File.with_user(self.outsider).search([('project_id', '=', self.project.id)]))
        self.assertEqual(File.with_user(self.manager).search([('project_id', '=', self.project.id)]), file)
        with self.assertRaises(AccessError):
            file.with_user(self.outsider).read(['share_url'])
        self.assertEqual(file.with_user(self.member).share_url, 'https://example.test/plan.pdf')

    def test_file_count_follows_visibility(self):
        self.make_file()
        self.assertEqual(self.project.with_user(self.member).dms_file_count, 1)
        self.assertEqual(self.project.with_user(self.outsider).dms_file_count, 0)

    def test_files_created_by_the_system_only(self):
        with self.assertRaises(AccessError):
            self.env['dms.file'].with_user(self.manager).create({
                'name': 'x.pdf', 'project_id': self.project.id, 'source': 'upload'})

    def test_protected_fields_need_the_system(self):
        file = self.make_file()
        for user in (self.member, self.manager):
            with self.assertRaises(AccessError):
                file.with_user(user).write({'share_url': 'https://elsewhere.test'})
            with self.assertRaises(AccessError):
                file.with_user(user).write({'state': 'removed'})
        file.sudo().write({'state': 'removed'})
        self.assertEqual(file.state, 'removed')

    def test_never_deleted_or_copied(self):
        file = self.make_file()
        with self.assertRaises(UserError):
            file.sudo().unlink()
        with self.assertRaises(UserError):
            file.sudo().copy()

    def test_one_shared_file_per_number(self):
        number = self.make_number('QTN-901/ZDMQB901/XI/26', self.code)
        first = self.make_file('a.pdf', number_id=number.id)
        with self.assertRaises(ValidationError):
            self.make_file('b.pdf', number_id=number.id)
        first.sudo().write({'state': 'superseded'})
        second = self.make_file('b.pdf', number_id=number.id)
        self.assertEqual(second.state, 'shared')
        with self.assertRaisesRegex(ValidationError, 'already has a shared file'):
            first.sudo().write({'state': 'shared'})

    def test_shared_index_holds_without_the_orm(self):
        number = self.make_number('QTN-902/ZDMQB901/XI/26', self.code)
        first = self.make_file('a.pdf', number_id=number.id, state='superseded')
        self.make_file('b.pdf', number_id=number.id)
        with mute_logger('odoo.sql_db'), self.assertRaises(IntegrityError):
            with self.env.cr.savepoint():
                self.env.cr.execute("UPDATE dms_file SET state = 'shared' WHERE id = %s", [first.id])

    def test_number_of_another_code_refused(self):
        number = self.make_number('QTN-903/ZDMQB902/XI/26', self.code_other)
        with self.assertRaises(ValidationError):
            self.make_file('c.pdf', number_id=number.id)


@tagged('post_install', '-at_install', 'dms')
class TestGrantsAndUsers(DmsCommon):

    def test_grants_kept_by_the_system(self):
        file = self.make_file()
        Grant = self.env['dms.file.grant']
        with self.assertRaises(AccessError):
            Grant.with_user(self.manager).create({'file_id': file.id, 'user_id': self.member.id, 'email': 'm@x.test'})
        grant = Grant.sudo().create({'file_id': file.id, 'user_id': self.member.id, 'email': 'm@x.test'})
        self.assertEqual(Grant.with_user(self.manager).search([('file_id', '=', file.id)]), grant)
        with self.assertRaises(AccessError):
            Grant.with_user(self.member).search([])
        with mute_logger('odoo.sql_db'), self.assertRaises(IntegrityError):
            with self.env.cr.savepoint():
                Grant.sudo().create({'file_id': file.id, 'user_id': self.member.id, 'email': 'm@x.test'})
                Grant.flush()

    def test_email_used_for_access(self):
        user = new_test_user(self.env, login='dms_no_address', groups='base.group_user')
        self.assertFalse(user._dms_email())  # a login without @ and no sign-in address
        user.sudo().dms_m365_email = ' Someone@Example.Test '
        self.assertEqual(user._dms_email(), 'someone@example.test')
        user.sudo().write({'dms_m365_email': False, 'login': 'login@example.test'})
        self.assertEqual(user._dms_email(), 'login@example.test')

    def test_upload_folder_id_set_by_the_system(self):
        admin = self.env.ref('base.user_admin')
        with self.assertRaises(AccessError):
            self.member.with_user(admin).write({'dms_intake_item_id': 'folder'})
        self.member.sudo().write({'dms_intake_item_id': 'folder'})
        self.assertEqual(self.member.sudo().dms_intake_item_id, 'folder')

    def test_users_cannot_set_their_own_sign_in(self):
        with self.assertRaises(AccessError):
            self.member.with_user(self.member).write({'dms_m365_email': 'someone-else@example.test'})
