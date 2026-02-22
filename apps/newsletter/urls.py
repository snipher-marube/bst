# newsletter/urls.py
from django.urls import path
from . import views

app_name = 'newsletter'

urlpatterns = [
    # Subscription endpoints
    path('api/subscribe/', views.newsletter_subscribe, name='api_subscribe'),
    path('confirm/<uuid:token>/', views.confirm_subscription, name='confirm'),
    path('unsubscribe/<uuid:token>/', views.unsubscribe, name='unsubscribe'),
    
    # Tracking endpoints
    path('track/open/<int:campaign_id>/', views.track_open, name='track_open'),
    path('track/open/<int:campaign_id>/<int:subscriber_id>/', views.track_open, name='track_open_subscriber'),
    path('track/click/<int:campaign_id>/<int:subscriber_id>/', views.track_click, name='track_click'),
    
    # Webhooks
    path('webhook/<str:provider>/', views.webhook, name='webhook'),
    
    # Admin preview
    path('preview/<int:campaign_id>/', views.campaign_preview, name='preview'),
]