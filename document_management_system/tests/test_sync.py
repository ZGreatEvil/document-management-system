# -*- coding: utf-8 -*-
import base64

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tests.common import new_test_user

from odoo.addons.document_management_system.models.graph_client import DmsGraphError

from .test_models import DmsCommon

NEW = 'dms_member2@example.test'


class SyncCommon(DmsCommon):

    def setUp(self):
        super().setUp()
        self.member2 = new_test_user(self.env, login='dms_member2', groups='base.group_user',
                                     company_id=self.company.id)
        self.member2.dms_m365_email = NEW
        self.add_file()

    def add_file(self, filename='plan.pdf', user=None, **values):
        vals = {'project_id': self.project.id, 'file': base64.b64encode(b'content'), 'filename': filename}
        vals.update(values)
        self.env['dms.file.add.wizard'].with_user(user or self.member).create(vals).action_add()
        # the newest file: a numbered one is renamed after its number
        return self.env['dms.file'].search([('project_id', '=', self.project.id)], order='id desc', limit=1)

    def shared_file(self):
        return self.env['dms.file'].search([('project_id', '=', self.project.id), ('state', '=', 'shared')], limit=1)

    def on_link(self, file):
        return self.graph.emails_on_link(file.sudo().item_id)

    def invite(self, user):
        self.project.write({'allowed_internal_user_ids': [(4, user.id)]})

    def uninvite(self, user):
        self.project.write({'allowed_internal_user_ids': [(3, user.id)]})

    def run_cron(self):
        self.env['project.project']._dms_cron_sync()


@tagged('post_install', '-at_install', 'dms')
class TestMemberSync(SyncCommon):

    def test_new_member_gets_access_on_the_next_run(self):
        file = self.shared_file()
        self.invite(self.member2)
        self.assertEqual(self.project.dms_sync_state, 'pending')
        self.assertNotIn(NEW, self.on_link(file))
        self.run_cron()
        self.assertIn(NEW, self.on_link(file))
        self.assertIn(self.member2, file.sudo().grant_ids.mapped('user_id'))
        self.assertEqual(self.project.dms_sync_state, 'ok')
        self.assertTrue(self.project.dms_last_sync)

    def test_member_who_leaves_is_revoked(self):
        file = self.shared_file()
        self.uninvite(self.member)
        self.run_cron()
        self.assertNotIn('dms_member@example.test', self.on_link(file))
        self.assertNotIn(self.member, file.sudo().grant_ids.mapped('user_id'))

    def test_unchanged_members_make_no_graph_call(self):
        self.graph.reset_calls()
        self.project._dms_sync_files()
        self.assertEqual([c for c in self.graph.calls if c[0] == 'POST'], [])

    def test_new_address_revokes_old_and_grants_new(self):
        file = self.shared_file()
        self.member.dms_m365_email = 'renamed@example.test'
        self.assertEqual(self.project.dms_sync_state, 'pending')
        self.run_cron()
        self.assertIn('renamed@example.test', self.on_link(file))
        self.assertNotIn('dms_member@example.test', self.on_link(file))
        self.assertEqual(file.sudo().grant_ids.filtered(lambda g: g.user_id == self.member).email,
                         'renamed@example.test')

    def test_deactivated_user_is_revoked(self):
        file = self.shared_file()
        self.invite(self.member2)
        self.run_cron()
        self.member2.active = False
        self.assertEqual(self.project.dms_sync_state, 'pending')
        self.run_cron()
        self.assertNotIn(NEW, self.on_link(file))

    def test_superseded_files_keep_their_access(self):
        number = self.make_number('BST-971/ZDMQB901/XI/26', self.code, self.type_bst, self.div_tec)
        first = self.add_file('a.pdf', number_id=number.id)
        number.sudo().write({'revision': 1})
        self.add_file('b.pdf', number_id=number.id, replace_current=True)
        self.invite(self.member2)
        self.run_cron()
        self.assertEqual(first.state, 'superseded')
        self.assertNotIn(NEW, self.on_link(first))
        self.assertIn('dms_member@example.test', self.on_link(first))

    def test_failure_is_kept_and_retried(self):
        self.invite(self.member2)
        self.graph.fail[('POST', '/permission/grant')] = DmsGraphError(503, 'serviceNotAvailable')
        self.run_cron()
        self.assertEqual(self.project.dms_sync_state, 'error')
        self.assertIn('serviceNotAvailable', self.project.sudo().dms_last_error)
        self.run_cron()
        self.assertEqual(self.project.dms_sync_state, 'ok')
        self.assertFalse(self.project.sudo().dms_last_error)
        self.assertIn(NEW, self.on_link(self.shared_file()))

    def test_grant_url_change_is_kept(self):
        file = self.shared_file()
        before = file.share_url
        self.invite(self.member2)
        self.run_cron()
        self.assertNotEqual(file.share_url, before)
        self.assertTrue(file.share_url.endswith('?granted?granted'))


@tagged('post_install', '-at_install', 'dms')
class TestSyncTriggers(SyncCommon):

    def test_new_internal_user_joins_projects_for_all_employees(self):
        self.project.write({'privacy_visibility': 'employees'})
        self.run_cron()
        user = new_test_user(self.env, login='dms_newcomer@example.test', groups='base.group_user',
                             company_id=self.company.id)
        self.assertIn(user, self.project.dms_member_user_ids)
        self.assertEqual(self.project.dms_sync_state, 'pending')

    def test_new_project_administrator_joins(self):
        self.env.ref('project.group_project_manager').write({'users': [(4, self.member2.id)]})
        self.assertIn(self.member2, self.project.dms_member_user_ids)

    def test_numbers_no_longer_decide_membership(self):
        self.make_number('BST-972/ZDMQB901/XI/26', self.code, self.type_bst, self.div_tec, self.member2)
        self.assertNotIn(self.member2, self.project.dms_member_user_ids)

    def test_other_company_user_never_joins(self):
        self.project.write({'privacy_visibility': 'employees'})
        stranger = new_test_user(self.env, login='dms_stranger', groups='base.group_user',
                                 company_id=self.company_b.id, company_ids=[(6, 0, self.company_b.ids)])
        stranger.dms_m365_email = 'stranger@example.test'
        self.assertNotIn(stranger, self.project.dms_member_user_ids)


@tagged('post_install', '-at_install', 'dms')
class TestCancelledNumber(SyncCommon):

    def test_cancelled_number_files_deleted_on_the_next_run(self):
        number = self.make_number('BST-981/ZDMQB901/XI/26', self.code, self.type_bst, self.div_tec)
        first = self.add_file('a.pdf', number_id=number.id)
        number.sudo().write({'revision': 1})
        second = self.add_file('b.pdf', number_id=number.id, replace_current=True)
        items = [first.sudo().item_id, second.sudo().item_id]
        self.project.sudo().write({'dms_sync_state': 'ok'})
        number._cancel('wrong customer')
        # cancelling never waits for sharepoint: the files go on the next run
        self.assertEqual(self.project.dms_sync_state, 'pending')
        self.assertEqual(second.state, 'shared')
        self.run_cron()
        self.assertEqual((first.state, second.state), ('removed', 'removed'))
        for item_id in items:
            self.assertNotIn(item_id, self.graph.items)
        self.assertFalse(second.sudo().grant_ids)
        self.assertIn('was cancelled', second.message_ids[0].body)
        # other files are untouched
        self.assertEqual(self.env['dms.file'].search([('name', '=', 'plan.pdf')]).state, 'shared')

    def test_cancelled_number_without_files_changes_nothing(self):
        number = self.make_number('BST-982/ZDMQB901/XI/26', self.code, self.type_bst, self.div_tec)
        self.project.sudo().write({'dms_sync_state': 'ok'})
        number._cancel('not needed')
        self.assertEqual(self.project.dms_sync_state, 'ok')

    def test_file_already_gone_still_marked(self):
        number = self.make_number('BST-983/ZDMQB901/XI/26', self.code, self.type_bst, self.div_tec)
        file = self.add_file('a.pdf', number_id=number.id)
        del self.graph.items[file.sudo().item_id]
        number._cancel('test')
        self.run_cron()
        self.assertEqual(file.state, 'removed')

    def test_failed_delete_is_retried(self):
        number = self.make_number('BST-984/ZDMQB901/XI/26', self.code, self.type_bst, self.div_tec)
        file = self.add_file('a.pdf', number_id=number.id)
        number._cancel('test')
        self.graph.fail[('DELETE', '/items/%s' % file.sudo().item_id)] = DmsGraphError(503, 'serviceNotAvailable')
        self.run_cron()
        self.assertEqual((file.state, self.project.dms_sync_state), ('shared', 'error'))
        self.run_cron()
        self.assertEqual((file.state, self.project.dms_sync_state), ('removed', 'ok'))


@tagged('post_install', '-at_install', 'dms')
class TestSyncNow(SyncCommon):

    def test_managers_only(self):
        with self.assertRaisesRegex(AccessError, 'administrators'):
            self.project.with_user(self.member).action_dms_sync_now()

    def test_success_notifies(self):
        self.invite(self.member2)
        action = self.project.with_user(self.manager).action_dms_sync_now()
        self.assertEqual(action['params']['type'], 'success')
        self.assertIn(NEW, self.on_link(self.shared_file()))

    def test_failure_explained(self):
        self.invite(self.member2)
        self.graph.fail[('POST', '/permission/grant')] = DmsGraphError(403, 'accessDenied')
        with self.assertRaisesRegex(UserError, 'tried again automatically'):
            self.project.with_user(self.manager).action_dms_sync_now()

    def test_project_without_code(self):
        project = self.env['project.project'].create({'name': 'No code', 'company_id': self.company.id})
        with self.assertRaisesRegex(UserError, 'no unique code'):
            project.with_user(self.manager).action_dms_sync_now()

    def test_crons_are_installed_and_active(self):
        for xmlid, interval in (('ir_cron_dms_member_sync', (5, 'minutes')), ('ir_cron_dms_drift_check', (1, 'days'))):
            cron = self.env.ref('document_management_system.%s' % xmlid)
            self.assertTrue(cron.active)
            self.assertEqual((cron.interval_number, cron.interval_type), interval)


@tagged('post_install', '-at_install', 'dms')
class TestDrift(SyncCommon):

    def test_foreign_links_deleted_and_logged(self):
        file = self.shared_file()
        self.graph.copy_link(file.sudo().item_id)
        self.graph.copy_link(file.sudo().item_id, scope='existingAccess')
        self.env['project.project']._dms_cron_drift()
        scopes = sorted(p['link']['scope'] for p in self.graph.perms[file.sudo().item_id] if p.get('link'))
        self.assertEqual(scopes, ['existingAccess', 'users'])
        self.assertIn('not made by Odoo were deleted', self.project.message_ids[0].body)

    def test_lost_odoo_link_is_made_again(self):
        file = self.shared_file()
        self.graph.perms[file.sudo().item_id] = []
        self.env['project.project']._dms_cron_drift()
        self.assertIn('dms_member@example.test', self.on_link(file))
        self.assertEqual(file.state, 'shared')
        self.assertIn('made again', self.project.message_ids[0].body)

    def test_file_gone_from_sharepoint(self):
        file = self.shared_file()
        del self.graph.items[file.sudo().item_id]
        self.env['project.project']._dms_cron_drift()
        self.assertEqual(file.state, 'error')
        self.assertIn('no longer in SharePoint', self.project.message_ids[0].body)

    def test_nothing_to_report_posts_nothing(self):
        before = len(self.project.message_ids)
        self.env['project.project']._dms_cron_drift()
        self.assertEqual(len(self.project.message_ids), before)


@tagged('post_install', '-at_install', 'dms')
class TestRemove(SyncCommon):

    def test_managers_only(self):
        with self.assertRaisesRegex(AccessError, 'administrators'):
            self.shared_file().with_user(self.member).action_dms_remove()

    def test_remove_deletes_links_and_keeps_the_file(self):
        file = self.shared_file()
        item_id = file.sudo().item_id
        self.graph.copy_link(item_id)
        file.with_user(self.manager).action_dms_remove()
        self.assertEqual(file.state, 'removed')
        self.assertFalse(file.sudo().grant_ids)
        self.assertFalse([p for p in self.graph.perms[item_id] if p.get('link')])
        self.assertIn(item_id, self.graph.items)
        self.assertIn('Removed by', file.message_ids[0].body)
        with self.assertRaises(UserError):
            file.with_user(self.member).action_open()

    def test_file_already_gone_can_still_be_removed(self):
        file = self.shared_file()
        del self.graph.items[file.sudo().item_id]
        file.with_user(self.manager).action_dms_remove()
        self.assertEqual(file.state, 'removed')

    def test_removed_number_file_frees_the_number(self):
        number = self.make_number('BST-976/ZDMQB901/XI/26', self.code, self.type_bst, self.div_tec)
        file = self.add_file('a.pdf', number_id=number.id)
        file.with_user(self.manager).action_dms_remove()
        self.assertTrue(file.name.startswith('BST-976_ZDMQB901_XI_26 (removed '))
        self.add_file('b.pdf', number_id=number.id)
        shared = self.env['dms.file'].search([('number_id', '=', number.id), ('state', '=', 'shared')])
        self.assertEqual(shared.name, 'BST-976_ZDMQB901_XI_26.pdf')

    def test_graph_failure_changes_nothing(self):
        file = self.shared_file()
        self.graph.fail[('GET', '/permissions')] = DmsGraphError(503, 'serviceNotAvailable')
        with self.assertRaisesRegex(UserError, 'could not be removed'):
            file.with_user(self.manager).action_dms_remove()
        self.assertEqual(file.state, 'shared')


@tagged('post_install', '-at_install', 'dms')
class TestUploaderAccessAfterMove(DmsCommon):

    def paste(self, name):
        intake_id = self.member.with_user(self.member)._dms_intake_folder()[0]
        item_id = self.graph.add_file(intake_id, name)
        url = self.graph.copy_link(item_id)
        return item_id, url

    def test_uploaders_direct_access_removed_groups_kept(self):
        item_id, url = self.paste('scan.pdf')
        own = self.graph.add_direct(item_id, email='dms_member@example.test')
        owners = self.graph.add_direct(item_id, group='Project Documents Owners', roles=('owner',))
        self.env['dms.file.add.wizard'].with_user(self.member).create(
            {'project_id': self.project.id, 'mode': 'link', 'link': url}).action_add()
        left = [p['id'] for p in self.graph.perms[item_id]]
        self.assertNotIn(own, left)
        self.assertIn(owners, left)
        self.assertIn('perm-%s' % item_id, left)

    def test_refused_delete_is_skipped(self):
        item_id, url = self.paste('scan2.pdf')
        self.graph.add_direct(item_id, email='dms_member@example.test')
        self.graph.fail[('DELETE', '/permissions/direct')] = DmsGraphError(400, 'invalidRequest')
        self.env['dms.file.add.wizard'].with_user(self.member).create(
            {'project_id': self.project.id, 'mode': 'link', 'link': url}).action_add()
        self.assertEqual(self.env['dms.file'].search([('name', '=', 'scan2.pdf')]).state, 'shared')
