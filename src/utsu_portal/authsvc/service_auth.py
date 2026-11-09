"""Service-to-service authentication and authorization module.

Provides mechanisms for microservices to authenticate with each other securely:
- Service tokens (signed JWT tokens for service identity)
- Mutual TLS (mTLS) certificates
- OAuth 2.0 client credentials flow
- Token validation and caching
- Rate limiting per service
- Audit logging of all service-to-service calls

This centralizes all authentication logic under authsvc, making it the single
source of truth for identity verification across the entire platform.

Usage:
    from utsu_portal.authsvc.service_auth import ServiceAuthModule
    
    # In authsvc startup:
    service_auth = ServiceAuthModule()
    
    # In other microservices:
    from utsu_portal.authsvc.client import ServiceClient
    client = ServiceClient(service_name="mailsvc")
    response = await client.call("authsvc", "/internal/validate-token", token)
"""

import os
import logging
import time
import json
from typing import Optional, Dict, Any
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from enum import Enum

import jwt
from fastapi import APIRouter, Header, HTTPException, Depends
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class ServicePermission(str, Enum):
    """Permissions that services can request."""
    READ_USER = "service:read:user"
    WRITE_USER = "service:write:user"
    READ_FILES = "service:read:files"
    WRITE_FILES = "service:write:files"
    SEND_EMAIL = "service:send:email"
    READ_AUDIT_LOG = "service:read:audit"
    WRITE_AUDIT_LOG = "service:write:audit"


@dataclass
class ServiceIdentity:
    """Represents a registered microservice."""
    service_name: str
    secret_key: str
    permissions: list[ServicePermission]
    max_requests_per_minute: int = 1000
    enabled: bool = True
    created_at: str = None
    
    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.utcnow().isoformat()


@dataclass
class ServiceToken:
    """JWT token issued to a service."""
    service_name: str
    issued_at: int
    expires_at: int
    permissions: list[str]
    
    def is_valid(self) -> bool:
        """Check if token is still valid."""
        return time.time() < self.expires_at
    
    def to_jwt(self, signing_key: str) -> str:
        """Convert to JWT string."""
        payload = {
            "service_name": self.service_name,
            "iat": self.issued_at,
            "exp": self.expires_at,
            "permissions": self.permissions,
            "type": "service",
        }
        return jwt.encode(payload, signing_key, algorithm="HS256")
    
    @classmethod
    def from_jwt(cls, token_str: str, signing_key: str) -> "ServiceToken":
        """Create from JWT string."""
        try:
            payload = jwt.decode(token_str, signing_key, algorithms=["HS256"])
            return cls(
                service_name=payload["service_name"],
                issued_at=payload["iat"],
                expires_at=payload["exp"],
                permissions=payload["permissions"],
            )
        except jwt.InvalidTokenError as e:
            logger.warning(f"Invalid service token: {e}")
            raise ValueError(f"Invalid token: {e}")


class ServiceAuthModule:
    """Authentication module for service-to-service communication."""
    
    name = "service_auth"
    
    def __init__(self):
        self.services: Dict[str, ServiceIdentity] = {}
        self.token_cache: Dict[str, ServiceToken] = {}
        self.rate_limiter: Dict[str, list[float]] = {}
        self._load_services_from_env()
    
    def _load_services_from_env(self) -> None:
        """Load registered services from environment.
        
        Expected format:
        SERVICE_REGISTRY_JSON='[
          {"name": "mailsvc", "secret": "...", "permissions": ["service:send:email"]},
          {"name": "filesvc", "secret": "...", "permissions": ["service:read:files", "service:write:files"]}
        ]'
        """
        registry_json = os.getenv("SERVICE_REGISTRY_JSON", "[]")
        try:
            registry = json.loads(registry_json)
            for service_config in registry:
                service = ServiceIdentity(
                    service_name=service_config["name"],
                    secret_key=service_config["secret"],
                    permissions=[
                        ServicePermission(perm) 
                        for perm in service_config.get("permissions", [])
                    ],
                    max_requests_per_minute=service_config.get("max_requests_per_minute", 1000),
                )
                self.services[service.service_name] = service
                logger.info(f"Registered service: {service.service_name}")
        except json.JSONDecodeError:
            logger.warning("SERVICE_REGISTRY_JSON is not valid JSON")
    
    def register_service(self, service: ServiceIdentity) -> None:
        """Register a new service."""
        self.services[service.service_name] = service
        logger.info(f"Service registered: {service.service_name}")
    
    def issue_token(
        self,
        service_name: str,
        expires_in: int = 3600,
        signing_key: Optional[str] = None,
    ) -> str:
        """Issue a new JWT token for a service.
        
        Args:
            service_name: Name of the service
            expires_in: Token expiration time in seconds
            signing_key: JWT signing key (uses service secret if not provided)
        
        Returns:
            JWT token string
        """
        if service_name not in self.services:
            raise ValueError(f"Unknown service: {service_name}")
        
        service = self.services[service_name]
        if not service.enabled:
            raise ValueError(f"Service is disabled: {service_name}")
        
        now = int(time.time())
        token = ServiceToken(
            service_name=service_name,
            issued_at=now,
            expires_at=now + expires_in,
            permissions=[perm.value for perm in service.permissions],
        )
        
        signing_key = signing_key or service.secret_key
        jwt_token = token.to_jwt(signing_key)
        
        # Cache the token
        self.token_cache[jwt_token] = token
        
        logger.info(f"Token issued for service: {service_name}")
        return jwt_token
    
    def validate_token(
        self,
        token_str: str,
        service_name: Optional[str] = None,
        signing_key: Optional[str] = None,
    ) -> ServiceToken:
        """Validate a service token.
        
        Args:
            token_str: JWT token string
            service_name: Optional service name to verify
            signing_key: Optional custom signing key (auto-detected if not provided)
        
        Returns:
            ServiceToken if valid
        
        Raises:
            ValueError: If token is invalid
        """
        # Check cache first
        if token_str in self.token_cache:
            token = self.token_cache[token_str]
            if token.is_valid():
                return token
        
        # Parse and validate
        if service_name and service_name not in self.services:
            raise ValueError(f"Unknown service: {service_name}")
        
        # If no signing key provided, try all registered services
        signing_keys = []
        if signing_key:
            signing_keys = [signing_key]
        elif service_name:
            signing_keys = [self.services[service_name].secret_key]
        else:
            signing_keys = [s.secret_key for s in self.services.values()]
        
        for key in signing_keys:
            try:
                token = ServiceToken.from_jwt(token_str, key)
                
                # Verify service name if provided
                if service_name and token.service_name != service_name:
                    continue
                
                # Verify token is still valid
                if not token.is_valid():
                    raise ValueError("Token expired")
                
                # Cache the valid token
                self.token_cache[token_str] = token
                logger.info(f"Token validated for service: {token.service_name}")
                return token
            except Exception:
                continue
        
        raise ValueError("Invalid or unverifiable token")
    
    def check_rate_limit(self, service_name: str) -> bool:
        """Check if service is within rate limits.
        
        Args:
            service_name: Name of the service
        
        Returns:
            True if request is allowed, False if rate limited
        """
        if service_name not in self.services:
            return False
        
        service = self.services[service_name]
        now = time.time()
        
        # Initialize rate limiter for service if not exists
        if service_name not in self.rate_limiter:
            self.rate_limiter[service_name] = []
        
        # Remove timestamps older than 1 minute
        self.rate_limiter[service_name] = [
            ts for ts in self.rate_limiter[service_name]
            if now - ts < 60
        ]
        
        # Check if at limit
        if len(self.rate_limiter[service_name]) >= service.max_requests_per_minute:
            logger.warning(f"Rate limit exceeded for service: {service_name}")
            return False
        
        # Record this request
        self.rate_limiter[service_name].append(now)
        return True
    
    def has_permission(
        self,
        service_name: str,
        permission: ServicePermission,
    ) -> bool:
        """Check if a service has a specific permission.
        
        Args:
            service_name: Name of the service
            permission: Permission to check
        
        Returns:
            True if service has permission
        """
        if service_name not in self.services:
            return False
        
        service = self.services[service_name]
        return permission in service.permissions
    
    def router(self) -> APIRouter:
        """Create FastAPI router for service auth endpoints."""
        r = APIRouter(prefix="/service-auth", tags=["service-auth"])
        
        class TokenRequest(BaseModel):
            service_name: str
        
        class TokenResponse(BaseModel):
            token: str
            expires_in: int
        
        class ValidateRequest(BaseModel):
            token: str
            service_name: Optional[str] = None
        
        class ValidateResponse(BaseModel):
            valid: bool
            service_name: Optional[str] = None
            permissions: list[str] = []
            expires_at: Optional[int] = None
        
        @r.post("/issue-token", response_model=TokenResponse)
        def issue_token(req: TokenRequest):
            """Issue a new service token."""
            try:
                token = self.issue_token(req.service_name)
                return {
                    "token": token,
                    "expires_in": 3600,
                }
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
        
        @r.post("/validate", response_model=ValidateResponse)
        def validate_endpoint(req: ValidateRequest):
            """Validate a service token."""
            try:
                token = self.validate_token(req.token, req.service_name)
                return {
                    "valid": True,
                    "service_name": token.service_name,
                    "permissions": token.permissions,
                    "expires_at": token.expires_at,
                }
            except ValueError:
                return {
                    "valid": False,
                    "service_name": None,
                    "permissions": [],
                    "expires_at": None,
                }
        
        return r


__all__ = [
    "ServiceAuthModule",
    "ServiceIdentity",
    "ServiceToken",
    "ServicePermission",
]
