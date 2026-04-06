from django.urls import path
from . import views

app_name = 'workspaces'

urlpatterns = [
    # Onboarding wizard (shown right after workspace creation)
    path('<uuid:workspace_id>/onboarding/', views.onboarding_wizard, name='onboarding'),
    path('<uuid:workspace_id>/onboarding/seed/', views.onboarding_seed, name='onboarding_seed'),
    path('<uuid:workspace_id>/onboarding/complete/', views.onboarding_complete, name='onboarding_complete'),

    # Membership management
    path('invite/<uuid:workspace_id>/', views.invite_member, name='invite_member'),
    path('invite/<str:token>/accept/', views.accept_invitation, name='accept_invitation'),
    path('<uuid:workspace_id>/invite/<uuid:invitation_id>/revoke/', views.revoke_invitation, name='revoke_invitation'),
    path('<uuid:workspace_id>/members/<int:user_id>/remove/', views.remove_member, name='remove_member'),
    path('<uuid:workspace_id>/members/<int:user_id>/role/', views.update_member_role, name='update_member_role'),
]
