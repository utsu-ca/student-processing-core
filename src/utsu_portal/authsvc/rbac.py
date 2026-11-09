"""Role-Based Access Control (RBAC) module for the authentication service.

Extends the auth service with fine-grained permission management, allowing:
- Role definitions with associated permissions
- User role assignments (can have multiple roles)
- Permission checks at the handler level or programmatically
- Audit logging of permission-based access decisions
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from fastapi import APIRouter, HTTPException, Depends, Header
from pydantic import BaseModel

from ..context import Context


class Permission(str, Enum):
    """Core permissions used across the portal."""
    # Auth permissions
    AUTH_CREATE_USER = "auth:create_user"
    AUTH_DELETE_USER = "auth:delete_user"
    AUTH_MODIFY_ROLES = "auth:modify_roles"
    AUTH_VERIFY_IDENTITY = "auth:verify_identity"
    
    # Admin permissions
    ADMIN_VIEW_AUDIT = "admin:view_audit"
    ADMIN_VIEW_USERS = "admin:view_users"
    ADMIN_EXPORT_DATA = "admin:export_data"
    
    # Form permissions
    FORMS_CREATE = "forms:create"
    FORMS_EDIT = "forms:edit"
    FORMS_DELETE = "forms:delete"
    
    # AGM permissions
    AGM_VIEW = "agm:view"
    AGM_EDIT = "agm:edit"
    AGM_VERIFY = "agm:verify"
    
    # Clubs permissions
    CLUBS_CREATE = "clubs:create"
    CLUBS_EDIT = "clubs:edit"
    CLUBS_DELETE = "clubs:delete"


@dataclass
class Role:
    """A role bundles related permissions."""
    name: str
    permissions: set[Permission]
    description: str = ""
    
    def has_permission(self, permission: Permission) -> bool:
        return permission in self.permissions


# Default role definitions
DEFAULT_ROLES = {
    "student": Role(
        name="student",
        permissions={Permission.AGM_VIEW, Permission.FORMS_CREATE},
        description="Default student role with read-only access"
    ),
    "admin": Role(
        name="admin",
        permissions=set(Permission),  # All permissions
        description="Full administrative access"
    ),
    "moderator": Role(
        name="moderator",
        permissions={
            Permission.ADMIN_VIEW_AUDIT,
            Permission.ADMIN_VIEW_USERS,
            Permission.AGM_EDIT,
            Permission.FORMS_EDIT,
            Permission.AGM_VERIFY,
        },
        description="Moderation and review capabilities"
    ),
    "staff": Role(
        name="staff",
        permissions={
            Permission.ADMIN_VIEW_USERS,
            Permission.FORMS_CREATE,
            Permission.FORMS_EDIT,
            Permission.ADMIN_EXPORT_DATA,
        },
        description="Staff member with data access"
    ),
}


class RoleAssignment(BaseModel):
    """User role assignment record."""
    user_id: str
    role: str
    assigned_at: str
    assigned_by: str


class RBACModule:
    """RBAC router and utilities for permission-based access control."""
    
    name = "rbac"

    def __init__(self, default_roles: dict[str, Role] | None = None):
        self.roles = default_roles or DEFAULT_ROLES

    def router(self, ctx: Context) -> APIRouter:
        r = APIRouter(prefix="/rbac", tags=["rbac"])

        def get_user_roles(uid: str) -> list[Role]:
            """Fetch all roles assigned to a user."""
            account = ctx.store.get("accounts", uid)
            if not account:
                return []
            role_names = account.get("roles", [])
            return [self.roles[name] for name in role_names if name in self.roles]

        def has_permission(uid: str, permission: Permission) -> bool:
            """Check if a user has a specific permission."""
            roles = get_user_roles(uid)
            return any(role.has_permission(permission) for role in roles)

        def require_permission(permission: Permission):
            """Dependency injection for route protection."""
            async def _check(authorization: str | None = Header(None)):
                uid = ctx.decode_token(authorization)
                if not uid or not has_permission(uid, permission):
                    raise HTTPException(
                        status_code=403,
                        detail=f"Missing required permission: {permission.value}"
                    )
                return uid
            return _check

        # Admin endpoints for role management
        @r.get("/roles")
        def list_roles():
            """List all available roles (public metadata only)."""
            return {
                name: {
                    "name": role.name,
                    "description": role.description,
                    "permissions": list(role.permissions)
                }
                for name, role in self.roles.items()
            }

        @r.get("/users/{uid}/roles")
        def user_roles(uid: str, authorization: str | None = Header(None)):
            """Get roles assigned to a user (requires permission or self-check)."""
            requester = ctx.decode_token(authorization)
            if requester != uid and not has_permission(requester, Permission.ADMIN_VIEW_USERS):
                raise HTTPException(status_code=403)
            
            roles = get_user_roles(uid)
            return {"roles": [role.name for role in roles]}

        @r.get("/users/{uid}/permissions")
        def user_permissions(uid: str, authorization: str | None = Header(None)):
            """Get all permissions a user has (requires permission or self-check)."""
            requester = ctx.decode_token(authorization)
            if requester != uid and not has_permission(requester, Permission.ADMIN_VIEW_USERS):
                raise HTTPException(status_code=403)
            
            roles = get_user_roles(uid)
            perms = set()
            for role in roles:
                perms.update(role.permissions)
            return {"permissions": sorted([p.value for p in perms])}

        @r.post("/users/{uid}/roles/{role_name}")
        def assign_role(
            uid: str,
            role_name: str,
            authorization: str | None = Header(None)
        ):
            """Assign a role to a user (requires auth:modify_roles)."""
            requester = ctx.decode_token(authorization)
            if not has_permission(requester, Permission.AUTH_MODIFY_ROLES):
                raise HTTPException(status_code=403)
            
            if role_name not in self.roles:
                raise HTTPException(status_code=400, detail=f"Unknown role: {role_name}")
            
            account = ctx.store.get("accounts", uid)
            if not account:
                raise HTTPException(status_code=404)
            
            roles = account.get("roles", [])
            if role_name not in roles:
                roles.append(role_name)
                ctx.store.update("accounts", uid, {"roles": roles})
                ctx.audit(f"Role assigned: {role_name} -> {uid}", requester, "RBAC")
            
            return {"roles": roles}

        @r.delete("/users/{uid}/roles/{role_name}")
        def revoke_role(
            uid: str,
            role_name: str,
            authorization: str | None = Header(None)
        ):
            """Revoke a role from a user (requires auth:modify_roles)."""
            requester = ctx.decode_token(authorization)
            if not has_permission(requester, Permission.AUTH_MODIFY_ROLES):
                raise HTTPException(status_code=403)
            
            account = ctx.store.get("accounts", uid)
            if not account:
                raise HTTPException(status_code=404)
            
            roles = account.get("roles", [])
            if role_name in roles:
                roles.remove(role_name)
                ctx.store.update("accounts", uid, {"roles": roles})
                ctx.audit(f"Role revoked: {role_name} <- {uid}", requester, "RBAC")
            
            return {"roles": roles}

        return r

    def get_permission_checker(self, ctx: Context) -> Callable[[str, Permission], bool]:
        """Returns a function to check if a user has a permission."""
        def check(uid: str, permission: Permission) -> bool:
            account = ctx.store.get("accounts", uid)
            if not account:
                return False
            role_names = account.get("roles", [])
            roles = [self.roles[name] for name in role_names if name in self.roles]
            return any(role.has_permission(permission) for role in roles)
        return check
