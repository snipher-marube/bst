"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
import sys
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

try:
    from apps.core.views import health_check
except ImportError:
    # Fallback health check if view doesn't exist
    from django.http import JsonResponse
    from django.views.decorators.csrf import csrf_exempt
    
    @csrf_exempt
    def health_check(request):
        return JsonResponse({"status": "healthy", "message": "Server is running"})

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('apps.core.urls')),
    path("accounts/", include("allauth.urls")),
    path('/newsletter/', include('apps.newsletter.urls', namespace='newsletter')),
    path('dashboard/', include('apps.dashboards.urls', namespace='dashboard')),
    # Dashboard API URLs
    path('api/', include('apps.dashboards.urls_api')),
]

# Debug Toolbar URLs (only in development and not in management commands)
if settings.DEBUG:
    # Check if we're not running a management command
    IS_MANAGEMENT_COMMAND = len(sys.argv) > 1 and sys.argv[1] in [
        'celery', 'migrate', 'makemigrations', 'shell', 'shell_plus', 'test'
    ]
    
    if not IS_MANAGEMENT_COMMAND:
        try:
            import debug_toolbar
            urlpatterns = [
                path('__debug__/', include(debug_toolbar.urls)),
            ] + urlpatterns
            print("🔧 Debug toolbar URLs added")
        except ImportError:
            pass

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
