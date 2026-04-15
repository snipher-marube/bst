from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import api

# Create a router for viewset
router = DefaultRouter()
router.register(r'widgets', api.WidgetViewSet, basename='api-widget')

urlpatterns = [
    # Include router URLs
    path('', include(router.urls)),
    
    # Dashboard widgets endpoints
    path('dashboards/<uuid:dashboard_id>/widgets/', 
         api.DashboardWidgetsAPIView.as_view(), 
         name='api-dashboard-widgets'),
    
    # Dashboard layout update
    path('dashboards/<uuid:dashboard_id>/update_layout/', 
         api.DashboardLayoutAPIView.as_view(), 
         name='api-dashboard-layout'),
    
    # Widget data refresh
    path('widgets/<uuid:widget_id>/data/', 
         api.WidgetDataAPIView.as_view(), 
         name='api-widget-data'),
    
    # List all dashboards
    path('dashboards/',
         api.DashboardListAPIView.as_view(),
         name='api-dashboards'),

    path('dashboards/<uuid:dashboard_id>/update_layout/',
         api.DashboardLayoutAPIView.as_view(),
         name='api-dashboard-layout'),

    # Dashboard filter-bar config
    path('dashboards/<uuid:dashboard_id>/filter-config/',
         api.DashboardFilterConfigAPIView.as_view(),
         name='api-dashboard-filter-config'),
]