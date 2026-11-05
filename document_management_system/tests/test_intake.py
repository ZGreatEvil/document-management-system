# -*- coding: utf-8 -*-
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tests.common import new_test_user

from odoo.addons.document_management_system.models.graph_client import PARAM_PREFIX, DmsGraphError

from .test_flows import only_test_addresses
from .test_models import DmsCommon


@tagged('post_install', '-at_install', 'dms')
class TestUploadFolder(DmsCommon):

    def open_folder(self, user=None):
        return self.env['res.users'].with_user(user or self.member).action_dms_upload_folder()

    def test_first_use_creates_the_folder_for_this_user_only(self):
        action = self.open_folder()
        folder_id = self.member.sudo().dms_intake_item_id
        self.assertEqual((action['type'], action['target']), ('ir.actions.act_url', 'new'))
        self.assertEqual(action['url'], 'https://sharepoint.test/view/%s' % folder_id)
        self.assertEqual(self.graph.items[folder_id]['parent'], 'intake-root')
        self.assertEqual(self.graph.items[folder_id]['name'], 'dms_member@example.test')
        # write access for this user only, sign-in required, no email
        self.assertEqual(self.graph.invites[folder_id], [('dms_member@example.test', ['write'], True, False)])

    def test_second_use_reuses_the_folder(self):
        self.open_folder()
        self.graph.reset_calls()
        self.open_folder()
        self.assertEqual([call[0] for call in self.graph.calls], ['GET'])
        self.assertEqual(len(self.graph.invites[self.member.sudo().dms_intake_item_id]), 1)

    def test_deleted_folder_is_made_again(self):
        self.open_folder()
        old_id = self.member.sudo().dms_intake_item_id
        del self.graph.items[old_id]
        self.open_folder()
        new_id = self.member.sudo().dms_intake_item_id
        self.assertNotEqual(new_id, old_id)
        self.assertEqual(len(self.graph.invites[new_id]), 1)

    def test_each_user_has_their_own_folder(self):
        self.open_folder(self.member)
        self.open_folder(self.outsider)
        self.assertNotEqual(self.member.sudo().dms_intake_item_id, self.outsider.sudo().dms_intake_item_id)

    def test_user_without_address_refused(self):
        user = new_test_user(self.env, login='dms_no_address_intake', groups='base.group_user')
        with self.assertRaisesRegex(UserError, 'no Microsoft 365 address'):
            self.open_folder(user)
        self.assertFalse(self.graph.calls)

    def test_missing_settings_explained(self):
        self.env['ir.config_parameter'].sudo().set_param(PARAM_PREFIX + 'intake_folder_id', '')
        with self.assertRaisesRegex(UserError, '"Intake" folder'):
            self.open_folder()

    def test_graph_failure_saves_nothing(self):
        self.graph.fail[('POST', '/invite')] = DmsGraphError(403, 'accessDenied')
        with self.assertRaisesRegex(UserError, 'could not be prepared'):
            self.open_folder()
        self.assertFalse(self.member.sudo().dms_intake_item_id)

    def test_button_on_project_and_wizard(self):
        self.assertEqual(self.project.with_user(self.member).action_dms_upload_folder()['type'], 'ir.actions.act_url')
        wizard = self.env['dms.file.add.wizard'].with_user(self.member).create({'project_id': self.project.id})
        self.assertEqual(wizard.action_upload_folder()['type'], 'ir.actions.act_url')


@tagged('post_install', '-at_install', 'dms')
class TestPasteLink(DmsCommon):

    def setUp(self):
        super().setUp()
        self.intake_id = self.member.with_user(self.member)._dms_intake_folder()[0]
        self.project_folder = self.project.sudo().dms_folder_item_id
        self.graph.reset_calls()

    def put_in_intake(self, name='scan 01.pdf', parent=None, **kwargs):
        item_id = self.graph.add_file(parent or self.intake_id, name, **kwargs)
        return item_id, self.graph.copy_link(item_id)

    def paste(self, url, user=None, **values):
        vals = {'project_id': self.project.id, 'mode': 'link', 'link': url}
        vals.update(values)
        return self.env['dms.file.add.wizard'].with_user(user or self.member).create(vals).action_add()

    def files(self):
        return self.env['dms.file'].search([('project_id', '=', self.project.id)])

    def test_file_moved_renamed_and_shared(self):
        item_id, url = self.put_in_intake('scan 01.pdf', size=2048)
        self.paste(url)
        file = self.files()
        self.assertEqual((file.name, file.source, file.original_filename), ('scan 01.pdf', 'link', 'scan 01.pdf'))
        self.assertEqual((file.size, file.mimetype, file.uploaded_by_id), (2048, 'application/pdf', self.member))
        self.assertEqual(file.sudo().item_id, item_id)
        self.assertEqual(self.graph.items[item_id]['parent'], self.project_folder)
        # the copied link is gone; only odoo's link remains, given to the members
        self.assertEqual([p['id'] for p in self.graph.perms[item_id]], ['perm-%s' % item_id])
        self.assertEqual(only_test_addresses(self.graph.emails_on_link(item_id)), {'dms_member@example.test'})
        self.assertEqual(file.share_url, 'https://sharepoint.test/link/%s?granted' % item_id)
        self.assertIn(self.member, file.sudo().grant_ids.mapped('user_id'))

    def test_numbered_file_named_after_the_number(self):
        number = self.make_number('BST-951/ZDMQB901/XI/26', self.code, self.type_bst, self.div_tec)
        item_id, url = self.put_in_intake('photo.JPG')
        self.paste(url, number_id=number.id)
        self.assertEqual(self.graph.items[item_id]['name'], 'BST-951_ZDMQB901_XI_26.JPG')
        self.assertEqual(self.files().number_id, number)

    def test_taken_name_gets_a_suffix(self):
        self.graph.add_file(self.project_folder, 'scan 01.pdf')
        item_id, url = self.put_in_intake('scan 01.pdf')
        self.paste(url)
        self.assertEqual(self.files().name, 'scan 01 1.pdf')
        self.assertEqual(self.graph.items[item_id]['name'], 'scan 01 1.pdf')

    def test_numbered_name_taken_refused_and_file_stays(self):
        number = self.make_number('BST-952/ZDMQB901/XI/26', self.code, self.type_bst, self.div_tec)
        self.graph.add_file(self.project_folder, 'BST-952_ZDMQB901_XI_26.pdf')
        item_id, url = self.put_in_intake('mine.pdf')
        with self.assertRaisesRegex(UserError, 'add a revision'):
            self.paste(url, number_id=number.id)
        self.assertEqual((self.graph.items[item_id]['parent'], self.graph.items[item_id]['name']),
                         (self.intake_id, 'mine.pdf'))
        self.assertFalse(self.files())

    def test_folder_link_refused(self):
        folder_id = self.graph.add_folder(self.intake_id, 'a folder')
        url = self.graph.copy_link(folder_id)
        with self.assertRaisesRegex(UserError, 'a folder is a folder'):
            self.paste(url)
        self.assertNotIn('PATCH', [call[0] for call in self.graph.calls])

    def test_file_outside_the_upload_folder_refused(self):
        _item_id, url = self.put_in_intake('elsewhere.pdf', parent=self.project_folder)
        with self.assertRaisesRegex(UserError, 'elsewhere.pdf is not in your upload folder'):
            self.paste(url)
        self.assertNotIn('PATCH', [call[0] for call in self.graph.calls])

    def test_file_in_a_subfolder_refused(self):
        subfolder = self.graph.add_folder(self.intake_id, 'sub')
        _item_id, url = self.put_in_intake('deep.pdf', parent=subfolder)
        with self.assertRaisesRegex(UserError, 'not in your upload folder'):
            self.paste(url)

    def test_someone_elses_upload_folder_refused(self):
        other_intake = self.outsider.with_user(self.outsider)._dms_intake_folder()[0]
        _item_id, url = self.put_in_intake('theirs.pdf', parent=other_intake)
        with self.assertRaisesRegex(UserError, 'not in your upload folder'):
            self.paste(url)

    def test_other_library_refused(self):
        _item_id, url = self.put_in_intake('foreign.pdf', drive='drive-other')
        with self.assertRaisesRegex(UserError, 'not in the project document library'):
            self.paste(url)

    def test_reused_link_refused(self):
        _item_id, url = self.put_in_intake('once.pdf')
        self.paste(url)
        with self.assertRaisesRegex(UserError, 'once.pdf has already been added'):
            self.paste(url)
        self.assertEqual(len(self.files()), 1)

    def test_unknown_link_refused(self):
        with self.assertRaisesRegex(UserError, 'does not lead to a file'):
            self.paste('https://sharepoint.test/nothing/here')

    def test_not_a_link_refused(self):
        with self.assertRaisesRegex(UserError, 'starts with https'):
            self.paste('scan 01.pdf')
        with self.assertRaisesRegex(UserError, 'Paste the link'):
            self.paste('   ')
        self.assertFalse(self.graph.calls)

    def test_user_without_upload_folder_told_to_open_it(self):
        _item_id, url = self.put_in_intake()
        with self.assertRaisesRegex(UserError, 'My upload folder'):
            self.paste(url, user=self.manager)

    def test_failure_after_the_move_puts_the_file_back(self):
        item_id, url = self.put_in_intake('scan 02.pdf')
        self.graph.fail[('POST', '/permission/grant')] = DmsGraphError(503, 'serviceNotAvailable')
        with self.assertRaises(UserError):
            self.paste(url)
        item = self.graph.items[item_id]
        self.assertEqual((item['parent'], item['name']), (self.intake_id, 'scan 02.pdf'))
        # odoo's own link is removed again, so nobody keeps access through it
        self.assertNotIn('perm-%s' % item_id, [p['id'] for p in self.graph.perms.get(item_id, [])])
        self.assertIn(item_id, self.graph.items)
        self.assertFalse(self.files())

    def test_existing_access_links_left_alone(self):
        item_id, url = self.put_in_intake('scan 03.pdf')
        self.graph.copy_link(item_id, scope='existingAccess')
        self.paste(url)
        kept = sorted(p['link']['scope'] for p in self.graph.perms[item_id])
        self.assertEqual(kept, ['existingAccess', 'users'])

    def test_number_checks_apply_before_anything_moves(self):
        number = self.make_number('BST-953/ZDMQB901/XI/26', self.code, self.type_bst, self.div_tec, state='cancelled')
        _item_id, url = self.put_in_intake()
        with self.assertRaisesRegex(UserError, 'cancelled'):
            self.paste(url, number_id=number.id)
        self.assertFalse(self.graph.calls)

    def test_replacing_a_numbered_file(self):
        number = self.make_number('BST-954/ZDMQB901/XI/26', self.code, self.type_bst, self.div_tec)
        _first, url = self.put_in_intake('v1.pdf')
        self.paste(url, number_id=number.id)
        number.sudo().write({'revision': 1})
        _second, url2 = self.put_in_intake('v2.pdf')
        with self.assertRaisesRegex(UserError, 'Replace the current file'):
            self.paste(url2, number_id=number.id)
        self.paste(url2, number_id=number.id, replace_current=True)
        self.assertEqual(sorted(self.files().mapped('state')), ['shared', 'superseded'])

    def test_non_member_refused(self):
        _item_id, url = self.put_in_intake()
        with self.assertRaisesRegex(AccessError, 'Only members'):
            self.paste(url, user=self.outsider)

    def test_wizard_name_preview_hidden_without_number_in_link_mode(self):
        wizard = self.env['dms.file.add.wizard'].with_user(self.member).create(
            {'project_id': self.project.id, 'mode': 'link'})
        self.assertIs(wizard.final_name, False)

    def test_wizard_name_preview_in_link_mode(self):
        number = self.make_number('BST-955/ZDMQB901/XI/26', self.code, self.type_bst, self.div_tec)
        wizard = self.env['dms.file.add.wizard'].with_user(self.member).create(
            {'project_id': self.project.id, 'mode': 'link', 'number_id': number.id})
        self.assertIn('BST-955_ZDMQB901_XI_26', wizard.final_name)
        self.assertIn('extension', wizard.final_name)
