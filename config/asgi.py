# config/asgi.py
import os
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
from channels.security.websocket import AllowedHostsOriginValidator
from django.urls import re_path

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

# Import your routing after setting the environment
from apps.insights import routing as insights_routing

application = ProtocolTypeRouter({
    # HTTP requests go to Django
    "http": get_asgi_application(),
    
    # WebSocket requests handled by Channels
    "websocket": AllowedHostsOriginValidator(
        AuthMiddlewareStack(
            URLRouter(
                insights_routing.websocket_urlpatterns
            )
        )
    ),
})