from django.urls import path
from . import views

app_name = 'workspaces'

urlpatterns = [
    path('invite/<uuid:workspace_id>/', views.invite_member, name='invite_member'),
    path('invite/<str:token>/accept/', views.accept_invitation, name='accept_invitation'),
    path('<uuid:workspace_id>/invite/<uuid:invitation_id>/revoke/', views.revoke_invitation, name='revoke_invitation'),
    path('<uuid:workspace_id>/members/<int:user_id>/remove/', views.remove_member, name='remove_member'),
    path('<uuid:workspace_id>/members/<int:user_id>/role/', views.update_member_role, name='update_member_role'),
]
