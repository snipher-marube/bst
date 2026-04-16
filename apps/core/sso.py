"""
apps/core/sso.py
================
SAML 2.0 SSO views and helpers for AnalyticsMeta.

Endpoints (mounted at /sso/<workspace_id>/):
    GET  metadata/   — SP metadata XML (share with the IdP admin)
    GET  login/      — Initiate SAML AuthnRequest → redirect to IdP
    POST acs/        — Assertion Consumer Service (receives SAML Response from IdP)
    GET  slo/        — Single Log-Out initiation

Auth flow:
    1. User visits /sso/<workspace_id>/login/
    2. We build a SAML AuthnRequest and redirect to the IdP SSO URL.
    3. IdP authenticates the user and POSTs a signed SAML Response to /sso/<workspace_id>/acs/.
    4. We validate the signature, extract the email attribute.
    5. We find or create a Django User for that email.
    6. We add the user to the workspace (if auto_provision=True).
    7. We log the user in and redirect to the dashboard.
"""

import logging

from django.conf import settings
from django.contrib.auth import get_user_model, login
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods

logger = logging.getLogger(__name__)
User = get_user_model()


# ── python3-saml request helper ──────────────────────────────────────────────

def _prepare_django_request(request):
    """
    Translate a Django HttpRequest into the dict shape that python3-saml expects.
    """
    return {
        'https':       'on' if request.is_secure() else 'off',
        'http_host':   request.META.get('HTTP_HOST', ''),
        'script_name': request.META.get('PATH_INFO', ''),
        'server_port': request.META.get('SERVER_PORT', '443' if request.is_secure() else '80'),
        'get_data':    request.GET.copy(),
        'post_data':   request.POST.copy(),
        'query_string': request.META.get('QUERY_STRING', ''),
    }


def _build_saml_settings(sso_config, request):
    """
    Construct the settings dict for python3-saml from an SSOConfiguration.
    """
    base_url     = f"{request.scheme}://{request.get_host()}"
    workspace_id = str(sso_config.workspace_id)
    acs_url      = f"{base_url}/sso/{workspace_id}/acs/"
    slo_url      = f"{base_url}/sso/{workspace_id}/slo/"
    metadata_url = f"{base_url}/sso/{workspace_id}/metadata/"

    sp_entity_id = sso_config.sp_entity_id or metadata_url
    sp_cert = getattr(settings, 'SAML_SP_CERT', '')
    sp_key  = getattr(settings, 'SAML_SP_KEY',  '')

    idp = {
        'entityId': sso_config.idp_entity_id,
        'singleSignOnService': {
            'url':     sso_config.idp_sso_url,
            'binding': 'urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect',
        },
        'x509cert': sso_config.idp_x509_cert,
    }
    if sso_config.idp_slo_url:
        idp['singleLogoutService'] = {
            'url':     sso_config.idp_slo_url,
            'binding': 'urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect',
        }

    return {
        'strict': not settings.DEBUG,
        'debug':  settings.DEBUG,
        'sp': {
            'entityId': sp_entity_id,
            'assertionConsumerService': {
                'url':     acs_url,
                'binding': 'urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST',
            },
            'singleLogoutService': {
                'url':     slo_url,
                'binding': 'urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect',
            },
            'NameIDFormat': 'urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress',
            'x509cert':  sp_cert,
            'privateKey': sp_key,
        },
        'idp': idp,
        'security': {
            'nameIdEncrypted':       False,
            'authnRequestsSigned':   bool(sp_key),
            'logoutRequestSigned':   False,
            'logoutResponseSigned':  False,
            'signMetadata':          bool(sp_key),
            'wantMessagesSigned':    False,
            'wantAssertionsSigned':  True,
            'wantAssertionsEncrypted': False,
            'wantNameIdEncrypted':   False,
            'allowRepeatAttributeName': True,
            'rejectDeprecatedAlgorithm': True,
        },
        'contactPerson': {
            'technical': {
                'givenName':    'AnalyticsMeta Support',
                'emailAddress': getattr(settings, 'DEFAULT_FROM_EMAIL',
                                        'support@analyticsmeta.io'),
            },
        },
        'organization': {
            'en-US': {
                'name':        'AnalyticsMeta',
                'displayname': 'AnalyticsMeta',
                'url':         base_url,
            },
        },
    }


def _get_auth(sso_config, request):
    """Return an initialised OneLogin_Saml2_Auth instance."""
    from onelogin.saml2.auth import OneLogin_Saml2_Auth
    return OneLogin_Saml2_Auth(
        _prepare_django_request(request),
        _build_saml_settings(sso_config, request),
    )


def _get_sso_config(workspace_id):
    """Return the active SSOConfiguration for a workspace or 404."""
    from apps.workspaces.models import SSOConfiguration
    return get_object_or_404(SSOConfiguration, workspace_id=workspace_id, is_active=True)


# ── Public endpoint: SP metadata ─────────────────────────────────────────────

@require_GET
def sso_metadata(request, workspace_id):
    """
    Return the SP metadata XML.
    Share this URL with your IdP admin so they can register AnalyticsMeta
    as a Service Provider.
    """
    sso_config = _get_sso_config(workspace_id)
    auth       = _get_auth(sso_config, request)
    saml_setts = auth.get_settings()
    metadata   = saml_setts.get_sp_metadata()
    errors     = saml_setts.validate_metadata(metadata)
    if errors:
        logger.error("SSO metadata validation errors workspace=%s: %s", workspace_id, errors)
        return HttpResponseBadRequest(f"SP metadata error: {errors}")
    return HttpResponse(metadata, content_type='application/xml; charset=utf-8')


# ── Initiate SSO login ────────────────────────────────────────────────────────

@require_GET
def sso_login(request, workspace_id):
    """
    Initiate a SAML AuthnRequest.
    The user is redirected to the IdP's SSO URL with the AuthnRequest attached.
    """
    sso_config = _get_sso_config(workspace_id)
    auth       = _get_auth(sso_config, request)
    return_to  = request.GET.get('next') or '/dashboard/analytics/'
    # Store return_to in session so ACS can redirect after login
    request.session['sso_return_to']     = return_to
    request.session['sso_workspace_id']  = str(workspace_id)
    sso_url = auth.login(return_to=return_to)
    logger.info("SSO login initiated workspace=%s user_agent=%s",
                workspace_id, request.META.get('HTTP_USER_AGENT', '')[:80])
    return HttpResponseRedirect(sso_url)


# ── Assertion Consumer Service ────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(['GET', 'POST'])
def sso_acs(request, workspace_id):
    """
    Assertion Consumer Service.

    The IdP POSTs the SAML Response here after authenticating the user.
    We validate the response, extract the user's email, find/create the
    Django User, add them to the workspace, and log them in.
    """
    from apps.workspaces.models import SSOConfiguration, WorkspaceMembership

    try:
        sso_config = get_object_or_404(SSOConfiguration, workspace_id=workspace_id, is_active=True)
    except Exception:
        return HttpResponseBadRequest('SSO not configured for this workspace.')

    auth = _get_auth(sso_config, request)
    auth.process_response()
    errors = auth.get_errors()

    if errors:
        reason = auth.get_last_error_reason() or ', '.join(errors)
        logger.warning("SSO ACS errors workspace=%s: %s | %s", workspace_id, errors, reason)
        return render(request, 'sso/error.html', {
            'title':   'SSO Authentication Failed',
            'message': reason,
            'login_url': f'/sso/{workspace_id}/login/',
        }, status=401)

    if not auth.is_authenticated():
        return render(request, 'sso/error.html', {
            'title':   'SSO Authentication Failed',
            'message': 'IdP did not confirm authentication.',
            'login_url': f'/sso/{workspace_id}/login/',
        }, status=401)

    # ── Extract user attributes from assertion ───────────────────────────
    attrs      = auth.get_attributes()
    name_id    = auth.get_nameid()

    def _attr(name, fallback=''):
        """Get first value of a SAML attribute, falling back to *fallback*."""
        val = attrs.get(name)
        return val[0] if val else fallback

    email = (
        _attr(sso_config.attribute_email)
        or name_id
        or ''
    ).strip().lower()

    if not email or '@' not in email:
        logger.warning("SSO ACS: no valid email in assertion workspace=%s name_id=%s attrs=%s",
                       workspace_id, name_id, list(attrs.keys()))
        return render(request, 'sso/error.html', {
            'title':   'SSO Error — No Email',
            'message': 'Your IdP did not provide an email address. '
                       'Check the attribute mapping in your SSO configuration.',
            'login_url': f'/sso/{workspace_id}/login/',
        }, status=400)

    first_name = _attr(sso_config.attribute_first_name)
    last_name  = _attr(sso_config.attribute_last_name)

    # ── Find or provision user ───────────────────────────────────────────
    user = User.objects.filter(email=email).first()

    if user is None:
        if not sso_config.auto_provision:
            return render(request, 'sso/error.html', {
                'title':   'Account Not Found',
                'message': f'No AnalyticsMeta account exists for {email}. '
                           'Contact your workspace admin.',
                'login_url': f'/sso/{workspace_id}/login/',
            }, status=403)

        # Auto-provision: create user with unusable password
        username = email.split('@')[0]
        # Ensure username uniqueness
        base, n = username, 1
        while User.objects.filter(username=username).exists():
            username = f"{base}{n}"
            n += 1

        user = User.objects.create_user(
            username=username,
            email=email,
            password=None,          # unusable — SSO-only account
            first_name=first_name,
            last_name=last_name,
        )
        logger.info("SSO auto-provisioned user=%s workspace=%s", email, workspace_id)

    else:
        # Update name fields if IdP supplies them
        changed = False
        if first_name and user.first_name != first_name:
            user.first_name = first_name
            changed = True
        if last_name and user.last_name != last_name:
            user.last_name = last_name
            changed = True
        if changed:
            user.save(update_fields=['first_name', 'last_name'])

    # ── Ensure workspace membership ──────────────────────────────────────
    workspace = sso_config.workspace
    if not workspace.members.filter(pk=user.pk).exists():
        WorkspaceMembership.objects.create(
            workspace=workspace,
            user=user,
            role='viewer',          # default role for SSO-provisioned users
        )
        logger.info("SSO added user=%s to workspace=%s as viewer", email, workspace_id)

    # ── Set current workspace in session (same convention as switch_workspace) ─
    request.session['current_workspace_id'] = str(workspace.id)

    # ── Log in ───────────────────────────────────────────────────────────
    login(request, user, backend='django.contrib.auth.backends.ModelBackend')
    logger.info("SSO login success user=%s workspace=%s", email, workspace_id)

    return_to = request.session.pop('sso_return_to', '/dashboard/analytics/')
    # Safety: only allow relative redirects
    if not return_to.startswith('/'):
        return_to = '/dashboard/analytics/'
    return HttpResponseRedirect(return_to)


# ── Single Log-Out (SLO) ─────────────────────────────────────────────────────

@require_GET
def sso_slo(request, workspace_id):
    """
    Initiate or receive Single Log-Out.
    Logs the user out of AnalyticsMeta and optionally redirects to the IdP SLO.
    """
    from django.contrib.auth import logout as django_logout

    try:
        sso_config = _get_sso_config(workspace_id)
        if sso_config.idp_slo_url:
            auth    = _get_auth(sso_config, request)
            slo_url = auth.logout()
            django_logout(request)
            return HttpResponseRedirect(slo_url)
    except Exception as exc:
        logger.warning("SSO SLO error workspace=%s: %s", workspace_id, exc)

    django_logout(request)
    return redirect('/accounts/login/')
