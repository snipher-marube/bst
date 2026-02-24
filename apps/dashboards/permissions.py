from rest_framework import permissions

class HasWorkspaceAccess(permissions.BasePermission):
    """
    Check if user has access to workspace
    """
    
    def has_permission(self, request, view):
        # Must be authenticated
        if not request.user or not request.user.is_authenticated:
            return False
        
        # Must have a current workspace
        workspace_id = request.headers.get('X-Workspace-ID') or request.GET.get('workspace')
        if not workspace_id:
            return False
        
        # Check if user is member of workspace
        from .models import Workspace
        try:
            workspace = Workspace.objects.get(id=workspace_id)
            return workspace.members.filter(id=request.user.id).exists()
        except Workspace.DoesNotExist:
            return False
    
    def has_object_permission(self, request, view, obj):
        # Object-level permission
        if hasattr(obj, 'workspace'):
            return obj.workspace.members.filter(id=request.user.id).exists()
        return False


class HasTableAccess(permissions.BasePermission):
    """
    Check if user can access a specific table
    """
    
    def has_object_permission(self, request, view, obj):
        from .models import DataTable
        
        if isinstance(obj, DataTable):
            return obj.workspace.members.filter(id=request.user.id).exists()
        
        if hasattr(obj, 'table'):
            return obj.table.workspace.members.filter(id=request.user.id).exists()
        
        return False


class CanEditData(permissions.BasePermission):
    """
    Check if user has edit permissions (owner, admin, editor)
    """
    
    def has_object_permission(self, request, view, obj):
        from .models import WorkspaceMembership
        
        # Get workspace
        if hasattr(obj, 'workspace'):
            workspace = obj.workspace
        elif hasattr(obj, 'table'):
            workspace = obj.table.workspace
        else:
            return False
        
        # Check role
        try:
            membership = WorkspaceMembership.objects.get(
                workspace=workspace,
                user=request.user
            )
            return membership.role in ['owner', 'admin', 'editor']
        except WorkspaceMembership.DoesNotExist:
            return False