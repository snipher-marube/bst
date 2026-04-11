import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import Workspace, WorkspaceMembership, WorkspaceInvitation
from .onboarding import OnboardingService, INDUSTRY_LABELS

logger = logging.getLogger(__name__)
User = get_user_model()


# ---------------------------------------------------------------------------
# Onboarding wizard
# ---------------------------------------------------------------------------

@login_required
def onboarding_wizard(request, workspace_id):
    """
    3-step onboarding wizard shown immediately after workspace creation.
    GET  → render wizard template
    """
    workspace = get_object_or_404(
        Workspace, id=workspace_id, owner=request.user, is_active=True
    )
    if workspace.onboarding_completed:
        return redirect('dashboard:home')

    industries = [
        {'key': k, 'label': v} for k, v in INDUSTRY_LABELS.items()
    ]
    return render(request, 'workspaces/onboarding.html', {
        'workspace': workspace,
        'industries': industries,
    })


@login_required
@require_POST
def onboarding_seed(request, workspace_id):
    """
    AJAX POST — called by the wizard at step 2.
    Seeds sample data for the chosen industry and returns JSON with the
    dashboard URL so the frontend can display it and advance to step 3.
    """
    workspace = get_object_or_404(
        Workspace, id=workspace_id, owner=request.user, is_active=True
    )

    industry = request.POST.get('industry', 'other').strip().lower()

    try:
        dashboard = OnboardingService.seed_workspace(workspace, industry, request.user)
        dashboard_url = f"/dashboard/dashboards/{dashboard.id}/"
        return JsonResponse({
            'ok': True,
            'dashboard_url': dashboard_url,
            'dashboard_name': dashboard.name,
            'table_name': dashboard.name.replace(' Dashboard', ''),
        })
    except Exception as exc:
        logger.exception("Onboarding seed failed for workspace %s: %s", workspace_id, exc)
        return JsonResponse({'ok': False, 'error': str(exc)}, status=500)


@login_required
@require_POST
def onboarding_complete(request, workspace_id):
    """
    Final step — marks onboarding as done and redirects to the main dashboard.
    Optionally sends team invitation emails if the user filled that field.
    """
    workspace = get_object_or_404(
        Workspace, id=workspace_id, owner=request.user, is_active=True
    )

    # Mark complete
    workspace.onboarding_completed = True
    workspace.save(update_fields=['onboarding_completed'])

    # Optional: send invite emails for comma-separated addresses
    invite_emails_raw = request.POST.get('invite_emails', '').strip()
    if invite_emails_raw:
        emails = [e.strip().lower() for e in invite_emails_raw.split(',') if e.strip()]
        for email in emails[:5]:  # cap at 5 invites during onboarding
            if not email:
                continue
            invitation, _ = WorkspaceInvitation.objects.get_or_create(
                workspace=workspace,
                email=email,
                defaults={'role': 'editor', 'invited_by': request.user},
            )
            accept_url = (
                f"{getattr(settings, 'SITE_URL', 'http://localhost:8000')}"
                f"/workspaces/invite/{invitation.token}/accept/"
            )
            try:
                send_mail(
                    subject=f"You're invited to join {workspace.name} on AnalyticsMeta",
                    message=(
                        f"Hi,\n\n"
                        f"{request.user.email} has invited you to join \"{workspace.name}\".\n\n"
                        f"Click to accept: {accept_url}\n\n"
                        f"This link expires in 7 days.\n\n— The AnalyticsMeta Team"
                    ),
                    from_email=getattr(settings, 'SUPPORT_EMAIL', settings.EMAIL_HOST_USER),
                    recipient_list=[email],
                    fail_silently=True,
                )
            except Exception as exc:
                logger.warning("Invite email failed for %s: %s", email, exc)

    messages.success(request, f'Welcome to {workspace.name}! Your workspace is ready.')
    return redirect('dashboard:home')


def _can_manage(user, workspace):
    """Return True if the user has admin or owner role in the workspace."""
    return WorkspaceMembership.objects.filter(
        workspace=workspace, user=user, role__in=['owner', 'admin']
    ).exists()


@login_required
@require_POST
def invite_member(request, workspace_id):
    """Send an email invitation to join a workspace."""
    workspace = get_object_or_404(Workspace, id=workspace_id, is_active=True)

    if not _can_manage(request.user, workspace):
        messages.error(request, 'You do not have permission to invite members.')
        return redirect('dashboard:members')

    email = request.POST.get('email', '').strip().lower()
    role = request.POST.get('role', 'viewer')

    if not email:
        messages.error(request, 'Email is required.')
        return redirect('dashboard:members')

    # Check member limit
    stats = workspace.get_usage_stats()
    if stats['members'] >= stats['members_limit']:
        messages.error(request, 'Team member limit reached. Upgrade your plan to add more members.')
        return redirect('dashboard:members')

    # Check if already a member
    if User.objects.filter(email=email, workspaces=workspace).exists():
        messages.warning(request, f'{email} is already a member of this workspace.')
        return redirect('dashboard:members')

    # Create or re-use invitation
    invitation, created = WorkspaceInvitation.objects.get_or_create(
        workspace=workspace,
        email=email,
        defaults={
            'role': role,
            'invited_by': request.user,
        }
    )

    if not created:
        # Refresh token and expiry
        invitation.role = role
        invitation.invited_by = request.user
        invitation.is_revoked = False
        invitation.is_accepted = False
        invitation.expires_at = timezone.now() + timezone.timedelta(days=7)
        invitation.save()

    # Build acceptance URL
    accept_url = f"{getattr(settings, 'SITE_URL', 'http://localhost:8000')}/workspaces/invite/{invitation.token}/accept/"

    try:
        send_mail(
            subject=f"You're invited to join {workspace.name} on AnalyticsMeta",
            message=(
                f"Hi,\n\n"
                f"{request.user.email} has invited you to join the workspace "
                f"\"{workspace.name}\" as {role}.\n\n"
                f"Click the link below to accept:\n{accept_url}\n\n"
                f"This link expires in 7 days.\n\n"
                f"— The AnalyticsMeta Team"
            ),
            from_email=getattr(settings, 'SUPPORT_EMAIL', settings.EMAIL_HOST_USER),
            recipient_list=[email],
            fail_silently=False,
        )
        messages.success(request, f'Invitation sent to {email}.')
    except Exception as e:
        logger.error("Failed to send invitation email to %s: %s — accept_url=%s", email, e, accept_url)
        messages.warning(request, 'Invitation created but email delivery failed. Please contact support to resend the invite.')

    return redirect('dashboard:members')


@login_required
def accept_invitation(request, token):
    """Accept a workspace invitation."""
    invitation = get_object_or_404(WorkspaceInvitation, token=token)

    if invitation.is_expired:
        messages.error(request, 'This invitation link has expired.')
        return redirect('dashboard:home')

    if invitation.is_revoked:
        messages.error(request, 'This invitation has been revoked.')
        return redirect('dashboard:home')

    if invitation.is_accepted:
        messages.info(request, 'This invitation has already been accepted.')
        return redirect('dashboard:home')

    # The logged-in user must match the invitation email (or we auto-match)
    if request.user.email.lower() != invitation.email.lower():
        messages.error(
            request,
            f'This invitation was sent to {invitation.email}. Please log in with that account.'
        )
        return redirect('account_login')

    success = invitation.accept(request.user)
    if success:
        request.session['current_workspace_id'] = str(invitation.workspace.id)
        messages.success(request, f'Welcome to {invitation.workspace.name}!')
        return redirect('dashboard:home')
    else:
        messages.error(request, 'Could not accept invitation.')
        return redirect('dashboard:home')


@login_required
@require_POST
def revoke_invitation(request, workspace_id, invitation_id):
    """Revoke a pending invitation."""
    workspace = get_object_or_404(Workspace, id=workspace_id)
    if not _can_manage(request.user, workspace):
        return JsonResponse({'error': 'Permission denied'}, status=403)

    invitation = get_object_or_404(WorkspaceInvitation, id=invitation_id, workspace=workspace)
    invitation.is_revoked = True
    invitation.save(update_fields=['is_revoked'])
    messages.success(request, f'Invitation to {invitation.email} revoked.')
    return redirect('dashboard:members')


@login_required
@require_POST
def remove_member(request, workspace_id, user_id):
    """Remove a member from the workspace."""
    workspace = get_object_or_404(Workspace, id=workspace_id)
    if not _can_manage(request.user, workspace):
        return JsonResponse({'error': 'Permission denied'}, status=403)

    membership = get_object_or_404(WorkspaceMembership, workspace=workspace, user_id=user_id)
    if membership.role == 'owner':
        messages.error(request, 'Cannot remove the workspace owner.')
        return redirect('dashboard:members')

    membership.delete()
    messages.success(request, 'Member removed.')
    return redirect('dashboard:members')


@login_required
@require_POST
def update_member_role(request, workspace_id, user_id):
    """Change a member's role."""
    workspace = get_object_or_404(Workspace, id=workspace_id)
    if not _can_manage(request.user, workspace):
        return JsonResponse({'error': 'Permission denied'}, status=403)

    membership = get_object_or_404(WorkspaceMembership, workspace=workspace, user_id=user_id)
    new_role = request.POST.get('role', 'viewer')
    if membership.role == 'owner':
        messages.error(request, 'Cannot change the owner role.')
        return redirect('dashboard:members')

    membership.role = new_role
    membership.save(update_fields=['role'])
    messages.success(request, f'Role updated to {new_role}.')
    return redirect('dashboard:members')
