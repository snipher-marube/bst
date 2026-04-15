"""
SSO URL patterns — mounted at /sso/ in config/urls.py.

Public endpoints (no login required):
    /sso/login/                       — Email-based IdP detection page
    /sso/<workspace_id>/metadata/     — SP metadata XML  (share with IdP admin)
    /sso/<workspace_id>/login/        — Initiate SAML AuthnRequest
    /sso/<workspace_id>/acs/          — Assertion Consumer Service
    /sso/<workspace_id>/slo/          — Single Log-Out
"""

from django.urls import path
from . import sso
from .sso_detect import sso_email_detect

app_name = 'sso'

urlpatterns = [
    # Generic SSO entry page (email → IdP detection)
    path('login/',                             sso_email_detect,    name='detect'),
    # Per-workspace SAML 2.0 endpoints
    path('<uuid:workspace_id>/metadata/',      sso.sso_metadata,    name='metadata'),
    path('<uuid:workspace_id>/login/',         sso.sso_login,       name='login'),
    path('<uuid:workspace_id>/acs/',           sso.sso_acs,         name='acs'),
    path('<uuid:workspace_id>/slo/',           sso.sso_slo,         name='slo'),
]
