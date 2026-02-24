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
            
            print(f"DEBUG Middleware - Workspace ID from session: {workspace_id}")  # Debug
            print(f"DEBUG Middleware - User: {request.user.email}")  # Debug
            
            if workspace_id:
                try:
                    request.user.current_workspace = Workspace.objects.get(
                        id=workspace_id,
                        members=request.user
                    )
                    print(f"DEBUG Middleware - Workspace found: {request.user.current_workspace.name}")  # Debug
                except Workspace.DoesNotExist:
                    print(f"DEBUG Middleware - Workspace {workspace_id} not found for user")  # Debug
                    request.user.current_workspace = None
            else:
                # Set default workspace (first one)
                workspace = Workspace.objects.filter(members=request.user).first()
                request.user.current_workspace = workspace
                if workspace:
                    print(f"DEBUG Middleware - Setting default workspace: {workspace.name}")  # Debug
                    request.session['current_workspace_id'] = str(workspace.id)
                    request.session.save()
                else:
                    print(f"DEBUG Middleware - No workspaces found for user")  # Debug
        
        response = self.get_response(request)
        return response