"""
Split the single "Organization Admin" role into Owner, Organization Admin,
Cycle Manager and Report Viewer.

For each organization:
  - Every existing admin (member of the old "Organization Admin" group, holder
    of a direct can_manage_organization permission, or superuser with a
    profile) becomes an Organization Admin.
  - The earliest-joined of those admins also becomes the Owner.
  - Non-admins who could create cycles for others become Cycle Managers.
  - Nobody becomes a Report Viewer automatically: the Owner or an Org Admin
    assigns that role to HR staff after deploying.

Direct per-user grants of these permissions (made by the old setup wizard)
are removed so that roles are the only source of access.
"""
from django.db import migrations

PERMISSION_NAMES = {
    'can_manage_billing': 'Can manage billing and subscription',
    'can_delete_organization': 'Can delete organization',
    'can_manage_owners': 'Can add and remove owners',
    'can_view_audit_log': 'Can view the audit log',
    'can_invite_members': 'Can invite team members',
    'can_manage_organization': 'Can manage organization settings',
    'can_manage_cycles': 'Can create and run review cycles for others',
    'can_view_all_reports': 'Can view all organization reports',
    'can_investigate_responses': 'Can view reviewer identities for investigations',
}

GROUP_PERMISSIONS = {
    'Organization Owner': [
        'can_manage_billing', 'can_delete_organization',
        'can_manage_owners', 'can_view_audit_log',
    ],
    'Organization Admin': ['can_manage_organization', 'can_invite_members', 'can_manage_cycles'],
    'Cycle Manager': ['can_manage_cycles'],
    'Report Viewer': ['can_view_all_reports', 'can_investigate_responses'],
    'Organization Member': [],
}

OLD_ADMIN_PERMISSIONS = [
    'can_invite_members', 'can_manage_organization',
    'can_delete_organization', 'can_view_all_reports',
]


def _setup(apps):
    Group = apps.get_model('auth', 'Group')
    Permission = apps.get_model('auth', 'Permission')
    ContentType = apps.get_model('contenttypes', 'ContentType')
    ct, _ = ContentType.objects.get_or_create(app_label='accounts', model='userprofile')
    perms = {}
    for codename, name in PERMISSION_NAMES.items():
        perms[codename], _ = Permission.objects.get_or_create(
            codename=codename, content_type=ct, defaults={'name': name}
        )
    return Group, perms


def forwards(apps, schema_editor):
    Group, perms = _setup(apps)
    Organization = apps.get_model('core', 'Organization')
    UserProfile = apps.get_model('accounts', 'UserProfile')

    old_admin_group = Group.objects.filter(name='Organization Admin').first()
    old_admin_ids = set(old_admin_group.user_set.values_list('id', flat=True)) if old_admin_group else set()
    direct_admin_ids = set(
        perms['can_manage_organization'].user_set.values_list('id', flat=True)
    )

    groups = {}
    for name, codenames in GROUP_PERMISSIONS.items():
        group, _ = Group.objects.get_or_create(name=name)
        group.permissions.set([perms[c] for c in codenames])
        groups[name] = group

    summary = []
    for org in Organization.objects.all().order_by('id'):
        profiles = list(
            UserProfile.objects.filter(organization=org)
            .select_related('user')
            .order_by('user__date_joined', 'user__id')
        )
        admins = [
            p.user for p in profiles
            if p.user.id in old_admin_ids or p.user.id in direct_admin_ids or p.user.is_superuser
        ]
        admin_ids = {u.id for u in admins}
        owner = admins[0] if admins else None

        for profile in profiles:
            user = profile.user
            user.groups.add(groups['Organization Member'])
            if user.id in admin_ids:
                user.groups.add(groups['Organization Admin'])
                if not user.is_staff:
                    user.is_staff = True
                    user.save(update_fields=['is_staff'])
                if not profile.can_create_cycles_for_others:
                    profile.can_create_cycles_for_others = True
                    profile.save(update_fields=['can_create_cycles_for_others'])
            elif profile.can_create_cycles_for_others:
                user.groups.add(groups['Cycle Manager'])
            if owner is not None and user.id == owner.id:
                user.groups.add(groups['Organization Owner'])

        summary.append(
            f"{org.name}: owner={owner.email if owner else 'none'}, admins={len(admins)}"
        )

    # Roles are now the only source of these permissions.
    for perm in perms.values():
        perm.user_set.clear()

    if summary:
        print('\n  Role migration: ' + '; '.join(summary))
        print('  No Report Viewers were assigned — grant that role to HR staff on the Team page.')


def backwards(apps, schema_editor):
    Group, perms = _setup(apps)
    admin_group, _ = Group.objects.get_or_create(name='Organization Admin')
    admin_group.permissions.set([perms[c] for c in OLD_ADMIN_PERMISSIONS])
    for name in ('Organization Owner', 'Report Viewer'):
        group = Group.objects.filter(name=name).first()
        if group:
            for user in group.user_set.all():
                user.groups.add(admin_group)
    Group.objects.filter(name__in=['Organization Owner', 'Cycle Manager', 'Report Viewer']).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0009_roles_mfa'),
        ('core', '0011_auditlog'),
        ('auth', '0012_alter_user_first_name_max_length'),
        ('contenttypes', '0002_remove_content_type_name'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
