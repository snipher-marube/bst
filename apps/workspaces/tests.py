"""
apps/workspaces/tests.py
========================
Tests for Workspace, WorkspaceMembership, WorkspaceInvitation, and OnboardingService.
"""
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.dashboards.factories import (
    UserFactory, WorkspaceFactory, WorkspaceMembershipFactory,
    WorkspaceInvitationFactory, DataTableFactory,
)
from apps.workspaces.models import Workspace, WorkspaceMembership, WorkspaceInvitation


# ---------------------------------------------------------------------------
# Workspace model
# ---------------------------------------------------------------------------

class TestWorkspaceModel(TestCase):

    def setUp(self):
        self.workspace = WorkspaceFactory(
            max_tables=5,
            max_records_per_table=1000,
            max_team_members=2,
        )

    def test_str(self):
        self.assertIn(self.workspace.name, str(self.workspace))
        self.assertIn(self.workspace.owner.email, str(self.workspace))

    def test_can_add_table_when_below_limit(self):
        self.assertTrue(self.workspace.can_add_table())

    def test_can_add_table_at_limit(self):
        # Create exactly max_tables active tables
        for _ in range(5):
            DataTableFactory(workspace=self.workspace)
        self.assertFalse(self.workspace.can_add_table())

    def test_get_usage_stats_empty_workspace(self):
        stats = self.workspace.get_usage_stats()
        self.assertEqual(stats['tables'], 0)
        self.assertEqual(stats['records'], 0)
        self.assertEqual(stats['tables_limit'], 5)
        self.assertEqual(stats['members_limit'], 2)

    def test_get_usage_stats_counts_tables_and_members(self):
        extra_user = UserFactory()
        WorkspaceMembershipFactory(workspace=self.workspace, user=extra_user, role='editor')
        DataTableFactory(workspace=self.workspace)
        DataTableFactory(workspace=self.workspace)

        stats = self.workspace.get_usage_stats()
        self.assertEqual(stats['tables'], 2)
        self.assertEqual(stats['members'], 2)
        self.assertEqual(stats['dashboards'], 0)

    def test_default_tier_is_free(self):
        self.assertEqual(self.workspace.tier, 'free')

    def test_is_active_defaults_true(self):
        self.assertTrue(self.workspace.is_active)

    def test_onboarding_completed_defaults_false(self):
        self.assertFalse(self.workspace.onboarding_completed)


# ---------------------------------------------------------------------------
# WorkspaceMembership
# ---------------------------------------------------------------------------

class TestWorkspaceMembership(TestCase):

    def test_unique_membership_constraint(self):
        ws   = WorkspaceFactory()
        user = UserFactory()
        WorkspaceMembership.objects.create(workspace=ws, user=user, role='editor')
        with self.assertRaises(Exception):
            WorkspaceMembership.objects.create(workspace=ws, user=user, role='viewer')

    def test_str_contains_role_and_email(self):
        membership = WorkspaceMembershipFactory(role='admin')
        self.assertIn('admin', str(membership))
        self.assertIn(membership.user.email, str(membership))

    def test_role_choices(self):
        roles = [r[0] for r in WorkspaceMembership.ROLE_CHOICES]
        self.assertIn('owner',  roles)
        self.assertIn('admin',  roles)
        self.assertIn('editor', roles)
        self.assertIn('viewer', roles)


# ---------------------------------------------------------------------------
# WorkspaceInvitation
# ---------------------------------------------------------------------------

class TestWorkspaceInvitation(TestCase):

    def setUp(self):
        self.workspace = WorkspaceFactory()
        self.invitee   = UserFactory(email='invitee@test.com')

    def test_token_auto_generated_on_save(self):
        invite = WorkspaceInvitation.objects.create(
            workspace=self.workspace, email='a@test.com', invited_by=self.workspace.owner
        )
        self.assertIsNotNone(invite.token)
        self.assertGreater(len(invite.token), 20)

    def test_expires_at_set_to_7_days(self):
        invite = WorkspaceInvitation.objects.create(
            workspace=self.workspace, email='b@test.com', invited_by=self.workspace.owner
        )
        delta = invite.expires_at - invite.invited_at
        self.assertAlmostEqual(delta.days, 7, delta=1)

    def test_is_expired_false_for_fresh_invite(self):
        invite = WorkspaceInvitation.objects.create(
            workspace=self.workspace, email='c@test.com', invited_by=self.workspace.owner
        )
        self.assertFalse(invite.is_expired)

    def test_is_expired_true_for_past_expiry(self):
        invite = WorkspaceInvitation.objects.create(
            workspace=self.workspace, email='d@test.com', invited_by=self.workspace.owner
        )
        invite.expires_at = timezone.now() - timezone.timedelta(hours=1)
        invite.save(update_fields=['expires_at'])
        self.assertTrue(invite.is_expired)

    def test_accept_creates_membership_and_marks_accepted(self):
        invite = WorkspaceInvitation.objects.create(
            workspace=self.workspace,
            email=self.invitee.email,
            role='editor',
            invited_by=self.workspace.owner,
        )
        result = invite.accept(self.invitee)
        self.assertTrue(result)
        invite.refresh_from_db()
        self.assertTrue(invite.is_accepted)
        self.assertIsNotNone(invite.accepted_at)
        self.assertTrue(
            WorkspaceMembership.objects.filter(
                workspace=self.workspace, user=self.invitee, role='editor'
            ).exists()
        )

    def test_accept_rejected_when_expired(self):
        invite = WorkspaceInvitation.objects.create(
            workspace=self.workspace,
            email=self.invitee.email,
            invited_by=self.workspace.owner,
        )
        invite.expires_at = timezone.now() - timezone.timedelta(hours=1)
        invite.save(update_fields=['expires_at'])
        self.assertFalse(invite.accept(self.invitee))
        self.assertFalse(
            WorkspaceMembership.objects.filter(
                workspace=self.workspace, user=self.invitee
            ).exists()
        )

    def test_accept_rejected_when_revoked(self):
        invite = WorkspaceInvitation.objects.create(
            workspace=self.workspace,
            email=self.invitee.email,
            invited_by=self.workspace.owner,
        )
        invite.is_revoked = True
        invite.save(update_fields=['is_revoked'])
        self.assertFalse(invite.accept(self.invitee))

    def test_accept_idempotent_second_call_returns_false(self):
        invite = WorkspaceInvitation.objects.create(
            workspace=self.workspace,
            email=self.invitee.email,
            invited_by=self.workspace.owner,
        )
        invite.accept(self.invitee)
        self.assertFalse(invite.accept(self.invitee))

    def test_str_representation(self):
        invite = WorkspaceInvitation.objects.create(
            workspace=self.workspace, email='e@test.com', invited_by=self.workspace.owner
        )
        self.assertIn(self.workspace.name, str(invite))
        self.assertIn('e@test.com', str(invite))


# ---------------------------------------------------------------------------
# OnboardingService
# ---------------------------------------------------------------------------

class TestOnboardingService(TestCase):

    def _seed(self, workspace, industry):
        from apps.workspaces.onboarding import OnboardingService
        with patch('apps.insights.tasks.broadcast_widget_update.delay'), \
             patch('apps.insights.tasks.notify_table_change.delay'):
            return OnboardingService.seed_workspace(workspace, industry, workspace.owner)

    def test_sales_industry_creates_correct_table(self):
        ws = WorkspaceFactory()
        dashboard = self._seed(ws, 'sales')
        self.assertIsNotNone(dashboard)
        self.assertTrue(ws.tables.filter(name='Sales Pipeline').exists())

    def test_finance_industry_creates_correct_table(self):
        ws = WorkspaceFactory()
        self._seed(ws, 'finance')
        self.assertTrue(ws.tables.filter(name='Monthly Budget Tracker').exists())

    def test_hr_industry_creates_correct_table(self):
        ws = WorkspaceFactory()
        self._seed(ws, 'hr')
        self.assertTrue(ws.tables.filter(name='Employee Directory').exists())

    def test_operations_industry_creates_correct_table(self):
        ws = WorkspaceFactory()
        self._seed(ws, 'operations')
        self.assertTrue(ws.tables.filter(name='Project Tracker').exists())

    def test_unknown_industry_falls_back_to_other(self):
        ws = WorkspaceFactory()
        self._seed(ws, 'unknown_xyz')
        ws.refresh_from_db()
        self.assertEqual(ws.industry, 'other')

    def test_seed_is_idempotent(self):
        ws = WorkspaceFactory()
        self._seed(ws, 'hr')
        self._seed(ws, 'hr')
        self.assertEqual(ws.tables.filter(name='Employee Directory').count(), 1)

    def test_seed_sets_industry_on_workspace(self):
        ws = WorkspaceFactory()
        self._seed(ws, 'sales')
        ws.refresh_from_db()
        self.assertEqual(ws.industry, 'sales')


# ---------------------------------------------------------------------------
# Workspace views (require login)
# ---------------------------------------------------------------------------

from django.test import Client


class TestWorkspaceViews(TestCase):
    """Test workspace HTTP views. Uses Django's test Client (session-based)."""

    def setUp(self):
        self.client = Client()
        self.user = UserFactory()
        self.client.force_login(self.user)
        self.workspace = WorkspaceFactory(owner=self.user)
        # Ensure owner membership exists and raise member limit for invite tests
        WorkspaceMembership.objects.get_or_create(
            workspace=self.workspace, user=self.user, defaults={'role': 'owner'}
        )
        self.workspace.max_team_members = 10
        self.workspace.save(update_fields=['max_team_members'])

    def test_onboarding_wizard_get(self):
        resp = self.client.get(f'/workspaces/{self.workspace.id}/onboarding/')
        self.assertEqual(resp.status_code, 200)

    def test_onboarding_wizard_redirects_if_completed(self):
        self.workspace.onboarding_completed = True
        self.workspace.save()
        resp = self.client.get(f'/workspaces/{self.workspace.id}/onboarding/')
        self.assertEqual(resp.status_code, 302)

    def test_onboarding_wizard_requires_login(self):
        c = Client()
        resp = c.get(f'/workspaces/{self.workspace.id}/onboarding/')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/accounts/', resp['Location'])

    def test_onboarding_seed_sales(self):
        with patch('apps.dashboards.models.broadcast_widget_update.delay'), \
             patch('apps.dashboards.models.notify_table_change.delay'):
            resp = self.client.post(
                f'/workspaces/{self.workspace.id}/onboarding/seed/',
                {'industry': 'sales'},
            )
        self.assertEqual(resp.status_code, 200)
        import json
        data = json.loads(resp.content)
        self.assertTrue(data.get('ok'))

    def test_onboarding_complete_marks_workspace(self):
        resp = self.client.post(
            f'/workspaces/{self.workspace.id}/onboarding/complete/',
        )
        self.assertEqual(resp.status_code, 302)
        self.workspace.refresh_from_db()
        self.assertTrue(self.workspace.onboarding_completed)

    def test_onboarding_complete_with_invite_emails(self):
        resp = self.client.post(
            f'/workspaces/{self.workspace.id}/onboarding/complete/',
            {'invite_emails': 'alice@test.com, bob@test.com'},
        )
        self.assertEqual(resp.status_code, 302)
        # Invitations should be created
        self.assertEqual(
            WorkspaceInvitation.objects.filter(workspace=self.workspace).count(), 2
        )

    def test_invite_member(self):
        resp = self.client.post(
            f'/workspaces/invite/{self.workspace.id}/',
            {'email': 'newmember@test.com', 'role': 'editor'},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(
            WorkspaceInvitation.objects.filter(
                workspace=self.workspace, email='newmember@test.com'
            ).exists()
        )

    def test_invite_member_missing_email(self):
        resp = self.client.post(
            f'/workspaces/invite/{self.workspace.id}/',
            {'role': 'editor'},
        )
        self.assertEqual(resp.status_code, 302)
        # No invitation created
        self.assertEqual(
            WorkspaceInvitation.objects.filter(workspace=self.workspace).count(), 0
        )

    def test_invite_non_manager_blocked(self):
        viewer = UserFactory()
        WorkspaceMembership.objects.create(
            workspace=self.workspace, user=viewer, role='viewer'
        )
        self.client.force_login(viewer)
        resp = self.client.post(
            f'/workspaces/invite/{self.workspace.id}/',
            {'email': 'hack@test.com', 'role': 'editor'},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(
            WorkspaceInvitation.objects.filter(email='hack@test.com').exists()
        )

    def test_accept_invitation(self):
        invite = WorkspaceInvitationFactory(workspace=self.workspace, email=self.user.email)
        resp = self.client.get(f'/workspaces/invite/{invite.token}/accept/')
        self.assertEqual(resp.status_code, 302)
        invite.refresh_from_db()
        self.assertTrue(invite.is_accepted)

    def test_accept_invitation_wrong_user(self):
        invite = WorkspaceInvitationFactory(workspace=self.workspace, email='other@test.com')
        resp = self.client.get(f'/workspaces/invite/{invite.token}/accept/')
        self.assertEqual(resp.status_code, 302)
        invite.refresh_from_db()
        self.assertFalse(invite.is_accepted)

    def test_accept_expired_invitation(self):
        invite = WorkspaceInvitationFactory(
            workspace=self.workspace,
            email=self.user.email,
            expires_at=timezone.now() - timezone.timedelta(days=1),
        )
        resp = self.client.get(f'/workspaces/invite/{invite.token}/accept/')
        self.assertEqual(resp.status_code, 302)
        invite.refresh_from_db()
        self.assertFalse(invite.is_accepted)

    def test_revoke_invitation(self):
        invite = WorkspaceInvitationFactory(workspace=self.workspace, email='revoke@test.com')
        resp = self.client.post(
            f'/workspaces/{self.workspace.id}/invite/{invite.id}/revoke/'
        )
        self.assertEqual(resp.status_code, 302)
        invite.refresh_from_db()
        self.assertTrue(invite.is_revoked)

    def test_remove_member(self):
        member = UserFactory()
        WorkspaceMembership.objects.create(
            workspace=self.workspace, user=member, role='editor'
        )
        resp = self.client.post(
            f'/workspaces/{self.workspace.id}/members/{member.id}/remove/'
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(
            WorkspaceMembership.objects.filter(
                workspace=self.workspace, user=member
            ).exists()
        )
