from rest_framework import permissions
import logging
from apps.workspaces.models import Workspace, WorkspaceMembership

logger = logging.getLogger(__name__)

class HasWorkspaceAccess(permissions.BasePermission):
    """
    Check if user has access to workspace
    """
    
    def has_permission(self, request, view):
        # Must be authenticated
        if not request.user or not request.user.is_authenticated:
            logger.warning("User not authenticated")
            return False
        
        # Try to get workspace from multiple sources
        _data = request.data if isinstance(request.data, dict) else {}
        workspace_id = (
            request.headers.get('X-Workspace-ID') or
            request.GET.get('workspace') or
            _data.get('workspace_id')
        )
        
        # If no workspace_id provided, try to get from user's current workspace
        if not workspace_id and hasattr(request.user, 'current_workspace') and request.user.current_workspace:
            workspace_id = str(request.user.current_workspace.id)
            logger.debug(f"Using current workspace from user: {workspace_id}")
        
        if not workspace_id:
            logger.warning("No workspace ID found in request")
            return False
        
        # Check if user is member of workspace
        from .models import Workspace
        try:
            workspace = Workspace.objects.get(id=workspace_id)
            has_access = workspace.members.filter(id=request.user.id).exists()
            if not has_access:
                logger.warning(f"User {request.user.id} not a member of workspace {workspace_id}")
            return has_access
        except Workspace.DoesNotExist:
            logger.warning(f"Workspace {workspace_id} does not exist")
            return False
        except Exception as e:
            logger.error(f"Error checking workspace access: {str(e)}")
            return False
    
    def has_object_permission(self, request, view, obj):
        # Object-level permission
        try:
            if hasattr(obj, 'workspace'):
                return obj.workspace.members.filter(id=request.user.id).exists()
            elif hasattr(obj, 'dashboard') and hasattr(obj.dashboard, 'workspace'):
                return obj.dashboard.workspace.members.filter(id=request.user.id).exists()
            elif hasattr(obj, 'table') and hasattr(obj.table, 'workspace'):
                return obj.table.workspace.members.filter(id=request.user.id).exists()
            return False
        except Exception as e:
            logger.error(f"Error in object permission: {str(e)}")
            return False


class CanEditData(permissions.BasePermission):
    """
    Check if user has edit permissions (owner, admin, editor)
    """
    
    def has_permission(self, request, view):
        # For create operations, check if user has edit rights in workspace
        if request.method == 'POST':
            # Try to get workspace from request
            _data = request.data if isinstance(request.data, dict) else {}
            workspace_id = (
                request.headers.get('X-Workspace-ID') or
                request.GET.get('workspace') or
                _data.get('workspace_id')
            )
            
            if not workspace_id and hasattr(request.user, 'current_workspace') and request.user.current_workspace:
                workspace_id = str(request.user.current_workspace.id)
            
            if workspace_id:
                try:
                    membership = WorkspaceMembership.objects.select_related('workspace').get(
                        workspace_id=workspace_id,
                        user=request.user
                    )
                    return membership.role in ['owner', 'admin', 'editor']
                except WorkspaceMembership.DoesNotExist:
                    return False
        
        return True
    
    def has_object_permission(self, request, view, obj):
        
        # For safe methods (GET, HEAD, OPTIONS), allow all workspace members
        if request.method in permissions.SAFE_METHODS:
            return True
        
        # Get workspace from the object
        try:
            if hasattr(obj, 'workspace'):
                workspace = obj.workspace
            elif hasattr(obj, 'dashboard') and hasattr(obj.dashboard, 'workspace'):
                workspace = obj.dashboard.workspace
            elif hasattr(obj, 'table') and hasattr(obj.table, 'workspace'):
                workspace = obj.table.workspace
            else:
                logger.warning(f"Cannot determine workspace from object {obj}")
                return False
            
            # Check role
            membership = WorkspaceMembership.objects.select_related('workspace').get(
                workspace=workspace,
                user=request.user
            )
            return membership.role in ['owner', 'admin', 'editor']
            
        except WorkspaceMembership.DoesNotExist:
            logger.warning(f"User {request.user.id} not a member of workspace")
            return False
        except Exception as e:
            logger.error(f"Error in edit permission: {str(e)}")
            return False


class CanManageWorkspace(permissions.BasePermission):
    """
    Restrict to workspace owner or admin.
    """

    def has_permission(self, request, view):
        _data = request.data if isinstance(request.data, dict) else {}
        workspace_id = (
            request.headers.get('X-Workspace-ID') or
            request.GET.get('workspace') or
            _data.get('workspace_id') or
            (str(request.user.current_workspace.id)
             if hasattr(request.user, 'current_workspace') and request.user.current_workspace
             else None)
        )
        if not workspace_id:
            return False
        try:
            membership = WorkspaceMembership.objects.select_related('workspace').get(
                workspace_id=workspace_id,
                user=request.user
            )
            return membership.role in ['owner', 'admin']
        except WorkspaceMembership.DoesNotExist:
            return False