from django.urls import path
from . import views

app_name = 'subscriptions'

urlpatterns = [
    # Mock / Stripe
    path('upgrade/<uuid:workspace_id>/<str:tier>/', views.upgrade_plan, name='upgrade'),
    path('webhook/stripe/', views.stripe_webhook, name='stripe_webhook'),

    # M-Pesa Daraja
    path('mpesa/stk-push/', views.mpesa_stk_push, name='mpesa_stk_push'),
    path('mpesa/callback/', views.mpesa_callback, name='mpesa_callback'),
    path('mpesa/status/<str:checkout_request_id>/', views.mpesa_payment_status, name='mpesa_status'),
]
