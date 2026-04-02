"""
apps/workspaces/models.py
==========================
Core data models for workspace management in AnalyticsMeta.

What is a workspace?
--------------------
A **workspace** is the top-level multi-tenancy boundary in AnalyticsMeta.
Every piece of data — tables, records, dashboards, team members — belongs to
exactly one workspace.  Users can own multiple workspaces and be members of
workspaces owned by others.

Model overview
--------------
::

    User (Django auth)
      ├── owned_workspaces  ──►  Workspace  (owner FK)
      │                              │
      │                              ├── WorkspaceMembership  (through table)
      │                              │         └── role: owner|admin|editor|viewer
      │                              │
      │                              └── WorkspaceInvitation  (pending invites)
      │
      └── workspaces  ◄──  Workspace.members  (M2M via WorkspaceMembership)

Tier system
-----------
Workspaces are gated by a ``tier`` field (free / starter / professional /
enterprise).  Each tier maps to resource limits stored in three integer
columns on the Workspace row:

* ``max_tables``            — how many DataTable rows are allowed
* ``max_records_per_table`` — row limit per DataTable
* ``max_team_members``      — how many members can be in the workspace

These limits are applied by ``Subscription.apply_plan_limits()`` in
``apps/subscriptions/models.py`` after a successful M-Pesa payment.

Session handling
----------------
The "currently active" workspace is tracked in two places:

1. ``request.user.current_workspace`` — a transient attribute set on the user
   object during each request (not stored in the DB).
2. ``request.session['current_workspace_id']`` — persisted in Redis so the
   active workspace survives across requests.

Both are managed by ``DashboardHomeView`` in ``apps/dashboards/views.py``.
"""

import uuid
import secrets
from django.db import models
from django.contrib.auth import get_user_model
from django.utils import timezone
import logging

logger = logging.getLogger(__name__)

User = get_user_model()


# ===========================================================================
# Workspace
# ===========================================================================

class Workspace(models.Model):
    """
    Top-level isolation boundary for all user data in AnalyticsMeta.

    Every DataTable, Record, Dashboard, and team membership belongs to one
    workspace.  Workspace rows are never soft-deleted — deleting a workspace
    cascades to all child objects via database FK constraints.

    Tier and limits
    ---------------
    The ``tier`` field records the current subscription level.  The three
    ``max_*`` integer columns store the actual enforced limits, which are set
    by ``Subscription.apply_plan_limits()`` after a successful payment.
    Reading limits from the workspace row (rather than the Plan row) means
    limit checks never require a JOIN to the subscriptions table.

    Default limits (free tier)
    ~~~~~~~~~~~~~~~~~~~~~~~~~~
    * max_tables:             5
    * max_records_per_table:  1,000
    * max_team_members:       1

    Fields
    ------
    id
        UUID primary key — avoids enumerable integer IDs in URLs.
    name
        Human-readable workspace name (max 100 chars).  Shown in the dashboard
        header and referenced in M-Pesa confirmation checks (users type it to
        confirm deletion).
    owner
        The user who created the workspace.  CASCADE deletion means deleting
        the owner account deletes all their workspaces and all child data.
    members
        M2M to User via ``WorkspaceMembership``.  Includes the owner (who has
        an ``'owner'`` role membership row created at workspace creation time).
    tier
        Current subscription tier.  Updated by ``apply_plan_limits()``.
    is_active
        ``False`` if the workspace has been suspended.  Currently unused in
        business logic but reserved for future account-standing enforcement.
    max_tables / max_records_per_table / max_team_members
        Denormalised from the active Plan — stored here for cheap limit checks
        without joining to the subscriptions app.

    Indexes
    -------
    * ``(owner, is_active)`` — used by workspace list queries for a given user.
    * ``(tier)``             — used for analytics queries across the tenant base.
    """

    TIER_CHOICES = [
        ('free',         'Free'),
        ('starter',      'Starter'),
        ('professional', 'Professional'),
        ('enterprise',   'Enterprise'),
    ]

    id    = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name  = models.CharField(max_length=100)
    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='owned_workspaces',
        help_text="The user who owns and administers this workspace.",
    )
    members = models.ManyToManyField(
        User,
        through='WorkspaceMembership',
        through_fields=('workspace', 'user'),
        related_name='workspaces',
        help_text="All users with access, including the owner.",
    )

    tier       = models.CharField(max_length=20, choices=TIER_CHOICES, default='free')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_active  = models.BooleanField(default=True)

    # Resource limits — denormalised from the active Plan for fast enforcement
    max_tables             = models.IntegerField(default=5)
    max_records_per_table  = models.IntegerField(default=1000)
    max_team_members       = models.IntegerField(default=1)

    class Meta:
        indexes = [
            models.Index(fields=['owner', 'is_active']),
            models.Index(fields=['tier']),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.owner.email})"

    def can_add_table(self) -> bool:
        """
        Return ``True`` if this workspace is below its table limit.

        Called before creating a new ``DataTable`` to enforce the plan's
        ``max_tables`` constraint.

        Returns
        -------
        bool
            ``True``  → table creation is allowed.
            ``False`` → limit reached; the view should redirect to billing.
        """
        return self.tables.count() < self.max_tables

    def get_usage_stats(self) -> dict:
        """
        Return a snapshot of current resource usage for this workspace.

        Used by the billing page, the settings page, and the dashboard home
        to render progress bars and usage counters.  Performs one query per
        table to count records (``sum`` of individual ``records.count()``
        calls) — acceptable for the expected workspace sizes but could be
        replaced with a single aggregation query if tables grow large.

        Returns
        -------
        dict with keys:

        ``tables``
            Number of active DataTable rows.
        ``tables_limit``
            The workspace's ``max_tables`` value.
        ``records``
            Total record count across all tables in this workspace.
        ``records_limit``
            Theoretical maximum (``max_records_per_table × max_tables``).
            Note: this is not strictly enforced per-workspace — each table
            has its own limit of ``max_records_per_table``.
        ``members``
            Current member count (including owner).
        ``members_limit``
            The workspace's ``max_team_members`` value.
        ``storage_bytes``
            Estimated storage usage in bytes (sum of
            ``DataTable.estimated_storage_bytes`` across all tables).
        ``dashboards``
            Number of Dashboard rows linked to this workspace.
        """
        tables         = self.tables.all()
        total_records  = sum(table.records.count() for table in tables)
        total_storage  = sum(table.estimated_storage_bytes for table in tables)

        return {
            'tables':        tables.count(),
            'tables_limit':  self.max_tables,
            'records':       total_records,
            'records_limit': self.max_records_per_table * self.max_tables,
            'members':       self.members.count(),
            'members_limit': self.max_team_members,
            'storage_bytes': total_storage,
            'dashboards':    self.dashboards.count(),
        }


# ===========================================================================
# WorkspaceMembership
# ===========================================================================

class WorkspaceMembership(models.Model):
    """
    Through-table linking a User to a Workspace with a role.

    Every user with access to a workspace — including the owner — has a
    ``WorkspaceMembership`` row.  The owner's row has ``role='owner'`` and is
    created automatically when a workspace is set up.

    Roles
    -----
    +----------+--------------------------------------------------------------+
    | Role     | Permissions (enforced in view / serialiser logic)            |
    +==========+==============================================================+
    | owner    | Full control — rename, delete workspace, manage members      |
    +----------+--------------------------------------------------------------+
    | admin    | Manage members and content, cannot delete workspace          |
    +----------+--------------------------------------------------------------+
    | editor   | Create / edit tables and records, cannot manage members      |
    +----------+--------------------------------------------------------------+
    | viewer   | Read-only access to tables, records, and dashboards          |
    +----------+--------------------------------------------------------------+

    Fields
    ------
    workspace
        The workspace this membership grants access to.
    user
        The user being granted access.
    role
        Access level (see table above).
    invited_by
        The user who invited this member.  ``SET_NULL`` so memberships survive
        if the inviting user's account is deleted.
    invited_at
        Auto-set to the row creation time.
    joined_at
        Set when the invitation is accepted (``WorkspaceInvitation.accept``).
        ``None`` for the owner whose membership is created directly without an
        invitation.

    Constraints
    -----------
    ``unique_together (workspace, user)`` — a user can only have one
    membership per workspace, preventing role confusion.

    Index
    -----
    ``(workspace, role)`` — used when filtering members by role (e.g. to
    find all admins for an invitation approval flow).
    """

    ROLE_CHOICES = [
        ('owner',  'Owner'),
        ('admin',  'Admin'),
        ('editor', 'Editor'),
        ('viewer', 'Viewer'),
    ]

    workspace  = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    user       = models.ForeignKey(User, on_delete=models.CASCADE)
    role       = models.CharField(max_length=20, choices=ROLE_CHOICES, default='viewer')
    invited_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='invited_members',
    )
    invited_at = models.DateTimeField(auto_now_add=True)
    joined_at  = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ['workspace', 'user']
        indexes = [
            models.Index(fields=['workspace', 'role']),
        ]

    def __str__(self) -> str:
        return f"{self.user.email} – {self.workspace.name} ({self.role})"


# ===========================================================================
# WorkspaceInvitation
# ===========================================================================

class WorkspaceInvitation(models.Model):
    """
    A time-limited, single-use email invitation to join a workspace.

    Flow
    ----
    ::

        Inviter creates WorkspaceInvitation
            → system sends email with link containing token
            → invitee clicks link → accept() called
            → WorkspaceMembership created
            → is_accepted = True, accepted_at = now()

    Security
    --------
    * The ``token`` is a 64-character URL-safe random string generated by
      ``secrets.token_urlsafe(48)``.  It is used as the lookup key in the
      accept URL — guessing it is computationally infeasible.
    * Tokens expire after **7 days** (``expires_at`` set in ``save()``).
    * Invitations can be revoked by the workspace owner at any time by setting
      ``is_revoked=True``.
    * The ``accept()`` method checks expiry, revocation, and double-acceptance
      before creating the membership row.

    Unique constraint
    -----------------
    ``unique_together (workspace, email)`` — prevents duplicate invitations
    to the same address.  If an invitation needs to be resent, the old row
    must be deleted or revoked first.

    Fields
    ------
    id
        UUID primary key.
    workspace
        The workspace the invitee is being invited to.
    email
        The email address the invitation was sent to.  Does not need to match
        an existing User at invite time — the user may sign up after receiving
        the link.
    role
        The role the invitee will receive on acceptance.
    token
        Auto-generated secure random token (set in ``save()`` if blank).
    invited_by
        The user who created the invitation.  ``SET_NULL`` so invitations
        survive if the inviter's account is deleted.
    expires_at
        Auto-set to 7 days from creation (set in ``save()`` if blank).
        Override before saving to change the expiry window.
    is_accepted / accepted_at
        Set by ``accept()`` on successful acceptance.
    is_revoked
        Set by an admin/owner to invalidate the invitation before it is used.

    Indexes
    -------
    * ``(token)``              — fast lookup in the accept URL handler.
    * ``(email, is_accepted)`` — used to list pending invitations for an email.
    """

    ROLE_CHOICES = WorkspaceMembership.ROLE_CHOICES

    id        = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        Workspace,
        on_delete=models.CASCADE,
        related_name='invitations',
    )
    email = models.EmailField()
    role  = models.CharField(max_length=20, choices=ROLE_CHOICES, default='viewer')
    token = models.CharField(max_length=64, unique=True, blank=True)

    invited_by  = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='sent_invitations',
    )
    invited_at  = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    expires_at  = models.DateTimeField(null=True, blank=True)

    is_accepted = models.BooleanField(default=False)
    is_revoked  = models.BooleanField(default=False)

    class Meta:
        unique_together = ['workspace', 'email']
        indexes = [
            models.Index(fields=['token']),
            models.Index(fields=['email', 'is_accepted']),
        ]

    def save(self, *args, **kwargs) -> None:
        """
        Auto-populate ``token`` and ``expires_at`` on first save.

        ``token``     — set once using ``secrets.token_urlsafe(48)`` (64 chars
                        after base64 URL encoding) if the field is blank.
        ``expires_at`` — set to 7 days from now if the field is blank.

        Both fields are only set when blank, so explicit overrides (e.g. in
        tests or admin) are preserved.
        """
        if not self.token:
            self.token = secrets.token_urlsafe(48)
        if not self.expires_at:
            self.expires_at = timezone.now() + timezone.timedelta(days=7)
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"Invite {self.email} → {self.workspace.name} ({self.role})"

    @property
    def is_expired(self) -> bool:
        """
        Return ``True`` if the invitation has passed its expiry date.

        Checked by ``accept()`` before creating a membership.  Also useful
        in templates to show an "expired" badge on the members page.
        """
        return bool(self.expires_at and timezone.now() > self.expires_at)

    def accept(self, user) -> bool:
        """
        Accept the invitation and create a ``WorkspaceMembership`` for *user*.

        Guards
        ------
        Returns ``False`` (without raising) if any of the following are true:

        * The invitation has expired (``is_expired``).
        * The invitation has been revoked (``is_revoked``).
        * The invitation has already been accepted (``is_accepted``).

        On success, creates (or gets) a ``WorkspaceMembership`` row with the
        invitation's role, marks the invitation as accepted, and returns
        ``True``.

        Parameters
        ----------
        user
            The ``User`` instance accepting the invitation.  Typically the
            currently logged-in user after clicking the invite link.

        Returns
        -------
        bool
            ``True`` if the membership was created (or already existed),
            ``False`` if the invitation is invalid.

        Notes
        -----
        Uses ``get_or_create`` for the membership so re-accepting an already
        accepted invitation (e.g. from a double-click) is safe and idempotent.
        """
        if self.is_expired or self.is_revoked or self.is_accepted:
            return False

        WorkspaceMembership.objects.get_or_create(
            workspace=self.workspace,
            user=user,
            defaults={
                'role':       self.role,
                'invited_by': self.invited_by,
                'joined_at':  timezone.now(),
            }
        )
        self.is_accepted = True
        self.accepted_at = timezone.now()
        self.save(update_fields=['is_accepted', 'accepted_at'])
        return True
