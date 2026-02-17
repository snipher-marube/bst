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
    
    # API endpoints
    path('api/newsletter-signup/', views.newsletter_signup, name='newsletter_signup'),
    
    # SEO
    path('sitemap.xml/', views.sitemap_view, name='sitemap'),
]