"""
apps/core/sso_detect.py
=======================
Email-based SSO IdP detection.

When a user visits /sso/login/ and submits their work email, we look up
whether their email domain matches a workspace with an active SSO
configuration and redirect them to the correct IdP.

If no SSO workspace is found for the domain the user is shown an error.
"""

from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods


@require_http_methods(['GET', 'POST'])
def sso_email_detect(request: HttpRequest) -> HttpResponse:
    """
    GET  — render the SSO email entry form.
    POST — detect IdP by email domain and redirect.
    """
    if request.method == 'GET':
        return render(request, 'sso/login.html', {
            'next': request.GET.get('next', ''),
        })

    email = request.POST.get('email', '').strip().lower()
    next_url = request.POST.get('next', '')

    if not email or '@' not in email:
        return render(request, 'sso/login.html', {
            'error': 'Please enter a valid work email address.',
            'email': email,
            'next':  next_url,
        })

    domain = email.rsplit('@', 1)[-1]

    # Look for active SSO configs whose members have an email on this domain.
    # We match against the workspace's owner email domain as a heuristic.
    # A more robust implementation would store allowed domains on SSOConfiguration.
    from apps.workspaces.models import SSOConfiguration
    from django.contrib.auth import get_user_model

    User = get_user_model()

    # Strategy: find active SSO configs where at least one workspace member
    # has an email matching the submitted domain.
    sso_qs = (
        SSOConfiguration.objects
        .filter(is_active=True,
                workspace__members__email__iendswith=f'@{domain}')
        .select_related('workspace')
        .distinct()
    )

    configs = list(sso_qs[:5])

    if not configs:
        return render(request, 'sso/login.html', {
            'error': (
                f'No SSO configuration found for @{domain}. '
                'Check with your workspace administrator or sign in with a password.'
            ),
            'email': email,
            'next':  next_url,
        })

    if len(configs) == 1:
        # Exactly one match — redirect directly
        ws_id   = configs[0].workspace_id
        url     = f'/sso/{ws_id}/login/'
        if next_url:
            url += f'?next={next_url}'
        return redirect(url)

    # Multiple workspaces on this domain — let user pick
    return render(request, 'sso/pick_workspace.html', {
        'configs':  configs,
        'email':    email,
        'next':     next_url,
    })
