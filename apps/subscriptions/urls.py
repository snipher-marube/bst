from django.urls import path
from . import views

app_name = 'subscriptions'

urlpatterns = [
    path('upgrade/<uuid:workspace_id>/<str:tier>/', views.upgrade_plan, name='upgrade'),
    path('webhook/stripe/', views.stripe_webhook, name='stripe_webhook'),
]
