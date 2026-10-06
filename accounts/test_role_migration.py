"""Data migration 0010: split the old Organization Admin role."""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class SplitAdminRolesMigrationTest(TransactionTestCase):
    migrate_to = [('accounts', '0010_split_admin_roles')]

    def setUp(self):
        executor = MigrationExecutor(connection)
        # Every app at its latest migration, except accounts just before 0010.
        self.migrate_from = [
            node for node in executor.loader.graph.leaf_nodes() if node[0] != 'accounts'
        ] + [('accounts', '0009_roles_mfa')]
        executor.migrate(self.migrate_from)
        apps = executor.loader.project_state(self.migrate_from).apps

        User = apps.get_model('auth', 'User')
        Group = apps.get_model('auth', 'Group')
        Permission = apps.get_model('auth', 'Permission')
        ContentType = apps.get_model('contenttypes', 'ContentType')
        Organization = apps.get_model('core', 'Organization')
        UserProfile = apps.get_model('accounts', 'UserProfile')

        ct, _ = ContentType.objects.get_or_create(app_label='accounts', model='userprofile')
        perms = {}
        for codename in ('can_invite_members', 'can_manage_organization',
                         'can_delete_organization', 'can_view_all_reports'):
            perms[codename], _ = Permission.objects.get_or_create(
                codename=codename, content_type=ct, defaults={'name': codename})
        old_admin = Group.objects.get_or_create(name='Organization Admin')[0]
        old_admin.permissions.set(perms.values())
        member_group = Group.objects.get_or_create(name='Organization Member')[0]

        org = Organization.objects.create(name='Kings')
        other_org = Organization.objects.create(name='Other')

        def user(name, org, joined_offset, **profile):
            from django.utils import timezone
            from datetime import timedelta
            u = User.objects.create(
                username=name, email=f'{name}@x.test',
                date_joined=timezone.now() + timedelta(days=joined_offset))
            UserProfile.objects.create(user=u, organization=org, **profile)
            return u

        # Setup-wizard admin: direct permissions, earliest → Owner
        self.setup_admin = user('setup', org, 0, can_create_cycles_for_others=True)
        self.setup_admin.user_permissions.add(
            perms['can_invite_members'], perms['can_manage_organization'], perms['can_view_all_reports'])
        # Later admin via group
        self.group_admin = user('later', org, 5, can_create_cycles_for_others=True)
        self.group_admin.groups.add(old_admin)
        # Member allowed to create cycles for others → Cycle Manager
        self.creator = user('creator', org, 6, can_create_cycles_for_others=True)
        self.creator.groups.add(member_group)
        # Plain member
        self.member = user('member', org, 7)
        self.member.groups.add(member_group)
        # Other org's only admin
        self.other_admin = user('otheradmin', other_org, 1)
        self.other_admin.groups.add(old_admin)

        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(self.migrate_to)

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())

    def roles(self, user_id):
        from django.contrib.auth.models import User
        return set(User.objects.get(pk=user_id).groups.values_list('name', flat=True))

    def test_roles_after_migration(self):
        from django.contrib.auth.models import User
        self.assertEqual(self.roles(self.setup_admin.pk),
                         {'Organization Owner', 'Organization Admin', 'Organization Member'})
        self.assertEqual(self.roles(self.group_admin.pk), {'Organization Admin', 'Organization Member'})
        self.assertEqual(self.roles(self.creator.pk), {'Cycle Manager', 'Organization Member'})
        self.assertEqual(self.roles(self.member.pk), {'Organization Member'})
        self.assertEqual(self.roles(self.other_admin.pk),
                         {'Organization Owner', 'Organization Admin', 'Organization Member'})

        setup = User.objects.get(pk=self.setup_admin.pk)
        # Direct grants removed; nobody keeps report access automatically.
        self.assertFalse(setup.user_permissions.exists())
        self.assertFalse(setup.has_perm('accounts.can_view_all_reports'))
        self.assertTrue(setup.has_perm('accounts.can_manage_billing'))
        later = User.objects.get(pk=self.group_admin.pk)
        self.assertTrue(later.has_perm('accounts.can_manage_organization'))
        self.assertFalse(later.has_perm('accounts.can_view_all_reports'))
        self.assertFalse(later.has_perm('accounts.can_delete_organization'))
