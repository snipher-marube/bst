import logging
from .models import Workspace

logger = logging.getLogger(__name__)


class CurrentWorkspaceMiddleware:
    """
    Sets `request.user.current_workspace` for every authenticated request.

    Resolution order:
      1. X-Workspace-ID request header
      2. current_workspace_id session key
      3. First workspace the user belongs to (fallback)
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            workspace_id = (
                request.headers.get('X-Workspace-ID')
                or request.session.get('current_workspace_id')
            )

            if workspace_id:
                try:
                    workspace = Workspace.objects.get(id=workspace_id, members=request.user)
                except Workspace.DoesNotExist:
                    logger.warning("Workspace %s not found for user %s", workspace_id, request.user.pk)
                    workspace = Workspace.objects.filter(members=request.user).first()
            else:
                workspace = Workspace.objects.filter(members=request.user).first()
                if workspace:
                    request.session['current_workspace_id'] = str(workspace.id)

            # `current_workspace` is declared at class level on CustomUser (or
            # set here as a plain attribute on auth.User at runtime).
            object.__setattr__(request.user, 'current_workspace', workspace)  # type: ignore[arg-type]

        return self.get_response(request)
