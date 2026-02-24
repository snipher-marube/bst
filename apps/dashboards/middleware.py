from .models import Workspace

class CurrentWorkspaceMiddleware:
    """
    Middleware to set current workspace on request user
    """
    def __init__(self, get_response):
        self.get_response = get_response
    
    def __call__(self, request):
        if request.user.is_authenticated:
            # Get workspace from header, session, or default
            workspace_id = (
                request.headers.get('X-Workspace-ID') or
                request.session.get('current_workspace_id')
            )
            
            if workspace_id:
                try:
                    request.user.current_workspace = Workspace.objects.get(
                        id=workspace_id,
                        members=request.user
                    )
                except Workspace.DoesNotExist:
                    request.user.current_workspace = None
            else:
                # Set default workspace (first one)
                workspace = Workspace.objects.filter(members=request.user).first()
                request.user.current_workspace = workspace
                if workspace:
                    request.session['current_workspace_id'] = str(workspace.id)
        
        return self.get_response(request)