from django.urls import path
from . import views

app_name = 'core'

urlpatterns = [
    # Main pages
    path('', views.HomeView.as_view(), name='home'),
    path('about/', views.AboutView.as_view(), name='about'),
    path('features/', views.FeaturesView.as_view(), name='features'),
    path('features/<slug:slug>/', views.FeatureDetailView.as_view(), name='feature_detail'),
    path('pricing/', views.PricingView.as_view(), name='pricing'),
    path('contact/', views.ContactView.as_view(), name='contact'),
    
    # Blog
    path('blog/', views.BlogListView.as_view(), name='blog_list'),
    path('blog/<slug:slug>/', views.BlogDetailView.as_view(), name='blog_detail'),
    
    # SEO
    path('sitemap.xml/', views.sitemap_view, name='sitemap'),

     # Health check endpoints
    path('health/', views.health_check_html, name='health_html'),
    path('health/json/', views.health_check, name='health_json'),
    path('health/liveness/', views.liveness_probe, name='liveness'),
    path('health/readiness/', views.readiness_probe, name='readiness'),
    path('offline/', views.offline_view, name='offline'),

    # Prometheus metrics scrape endpoint (restrict at reverse-proxy in prod)
    path('metrics/', views.metrics_view, name='metrics'),
]