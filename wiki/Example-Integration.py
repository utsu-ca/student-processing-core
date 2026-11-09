"""Example: Integrating Observability, File Handler, and Audit Compliance

This file shows how to use the new systems in a real microservice.
"""

import logging
from fastapi import FastAPI, Header, HTTPException, UploadFile, File
from typing import Optional

# 1. UNIFIED OBSERVABILITY
from utsu_portal.observability import (
    setup_observability,
    get_tracer,
    get_logger,
    instrument_fastapi,
)

# 2. CLOUD-AGNOSTIC FILE HANDLER
from utsu_portal.filesvc import FileHandler

# 3. CENTRALIZED AUTHENTICATION
from utsu_portal.authsvc.service_auth import ServiceAuthModule, ServicePermission

# 4. AUDIT COMPLIANCE
from utsu_portal.authsvc.audit_compliance import (
    build_audit_entry_read,
    build_audit_entry_write,
    AuditAction,
)

# ============================================================================
# 1. SETUP OBSERVABILITY
# ============================================================================

# Initialize observability at service startup
setup_observability(
    service_name="mailsvc",
    service_version="1.0.0",
    jaeger_enabled=True,
    prometheus_enabled=True,
)

# Get loggers and tracers
logger = get_logger(__name__)
tracer = get_tracer(__name__)

# ============================================================================
# 2. CREATE FASTAPI APP WITH INSTRUMENTATION
# ============================================================================

app = FastAPI(title="Mail Service", version="1.0.0")

# Auto-instrument FastAPI (adds spans for every request)
instrument_fastapi(app)

# ============================================================================
# 3. INITIALIZE FILE HANDLER
# ============================================================================

# Will auto-detect backend from FILE_STORAGE environment variable
file_handler = FileHandler.from_env()

# ============================================================================
# 4. INITIALIZE SERVICE AUTHENTICATION
# ============================================================================

service_auth = ServiceAuthModule()

# ============================================================================
# 5. EXAMPLE ENDPOINT: Send Email with Full Audit Trail
# ============================================================================

class SendEmailRequest:
    recipient: str
    subject: str
    body: str


@app.post("/api/send-email")
async def send_email(
    req: SendEmailRequest,
    user_id: str = Header(),
    authorization: str = Header(),
):
    """
    Send an email with full observability, auth, and audit logging.
    
    Demonstrates:
    1. Distributed tracing
    2. Structured logging with trace context
    3. Service authentication (if called by another service)
    4. Audit logging for compliance
    """
    
    # Start tracing this operation
    with tracer.start_as_current_span("send_email") as span:
        try:
            # Log with automatic trace context
            logger.info(
                "Sending email",
                extra={
                    "user_id": user_id,
                    "recipient": req.recipient,
                    "subject": req.subject,
                },
            )
            
            # Check if this is a service-to-service call
            if authorization.startswith("Bearer "):
                token = service_auth.validate_token(
                    authorization.replace("Bearer ", "")
                )
                
                # Service must have permission to send email
                if ServicePermission.SEND_EMAIL not in token.permissions:
                    logger.warning(f"Service {token.service_name} lacks email permission")
                    raise HTTPException(403, "Insufficient permissions")
                
                actor_id = token.service_name
                actor_type = "service"
            else:
                actor_id = user_id
                actor_type = "user"
            
            # Rate limiting for services
            if actor_type == "service":
                if not service_auth.check_rate_limit(actor_id):
                    logger.warning(f"Rate limit exceeded for {actor_id}")
                    raise HTTPException(429, "Rate limited")
            
            # Send the email (add timing to trace)
            with tracer.start_as_current_span("smtp_send"):
                # TODO: Actual email sending logic
                logger.info(f"Email sent to {req.recipient}")
            
            # Audit log: record that email was sent
            audit_entry = build_audit_entry_write(
                actor_id=actor_id,
                actor_type=actor_type,
                resource_type="email",
                resource_id=f"email-{req.recipient}",
                action=AuditAction.CREATE,
                data_after={
                    "recipient": req.recipient,
                    "subject": req.subject,
                    "sent_at": "2024-11-09T23:30:00Z",
                },
            )
            # TODO: await store_audit_log(audit_entry)
            
            return {"status": "sent", "recipient": req.recipient}
        
        except Exception as e:
            logger.error(f"Failed to send email: {e}")
            # Add error context to trace
            span.record_exception(e)
            raise


# ============================================================================
# 6. EXAMPLE ENDPOINT: Upload File with Multi-Cloud Support
# ============================================================================

@app.post("/api/upload")
async def upload_file(
    file: UploadFile = File(),
    user_id: str = Header(),
    authorization: str = Header(),
):
    """
    Upload a file to cloud storage.
    
    Works identically on:
    - Local dev (stores in /tmp)
    - AWS (stores in S3 bucket)
    - GCP (stores in GCS bucket)
    - Azure (stores in Blob Storage)
    - FTP (stores on legacy server)
    
    Demonstrates:
    1. Cloud-agnostic file handling
    2. Audit logging for compliance
    3. Signed URLs for secure direct access
    """
    
    with tracer.start_as_current_span("upload_file") as span:
        try:
            # Validate authentication
            if authorization.startswith("Bearer "):
                token = service_auth.validate_token(
                    authorization.replace("Bearer ", "")
                )
                
                if ServicePermission.WRITE_FILES not in token.permissions:
                    raise HTTPException(403, "Cannot upload files")
                
                actor_id = token.service_name
                actor_type = "service"
            else:
                actor_id = user_id
                actor_type = "user"
            
            # Log upload attempt
            logger.info(
                f"Uploading file {file.filename}",
                extra={
                    "user_id": user_id,
                    "filename": file.filename,
                    "actor": actor_id,
                },
            )
            
            # Upload using cloud-agnostic handler
            file_key = file_handler.upload(
                file.file,
                f"uploads/{user_id}/{file.filename}",
            )
            
            logger.info(f"File stored at {file_key}")
            
            # Get a signed URL for direct access
            signed_url = file_handler.get_signed_url(file_key, expires_in=3600)
            
            # Audit log: record file upload
            audit_entry = build_audit_entry_write(
                actor_id=actor_id,
                actor_type=actor_type,
                resource_type="file",
                resource_id=file_key,
                action=AuditAction.UPLOAD,
                data_after={
                    "filename": file.filename,
                    "file_key": file_key,
                    "size_bytes": file.size,
                },
            )
            # TODO: await store_audit_log(audit_entry)
            
            return {
                "status": "uploaded",
                "file_key": file_key,
                "signed_url": signed_url,
                "expires_in": 3600,
            }
        
        except Exception as e:
            logger.error(f"Upload failed: {e}")
            span.record_exception(e)
            raise


# ============================================================================
# 7. EXAMPLE ENDPOINT: Download File with Read Audit Trail
# ============================================================================

@app.get("/api/download/{file_key:path}")
async def download_file(
    file_key: str,
    user_id: str = Header(),
):
    """
    Download a file with audit logging for compliance.
    
    Demonstrates:
    1. Permission checking before download
    2. Audit logging of data access
    3. Cloud-agnostic download
    """
    
    with tracer.start_as_current_span("download_file"):
        try:
            # Check if file exists
            if not file_handler.exists(file_key):
                logger.warning(f"File not found: {file_key}")
                
                # Audit log: denied access (file not found)
                audit_entry = build_audit_entry_read(
                    actor_id=user_id,
                    actor_type="user",
                    resource_type="file",
                    resource_id=file_key,
                    permission_granted=False,
                    reason="File not found",
                )
                # TODO: await store_audit_log(audit_entry)
                
                raise HTTPException(404, "File not found")
            
            # Permission check
            # TODO: Check if user owns this file or has read permission
            
            logger.info(f"Downloading file {file_key}", extra={"user_id": user_id})
            
            # Audit log: granted access
            audit_entry = build_audit_entry_read(
                actor_id=user_id,
                actor_type="user",
                resource_type="file",
                resource_id=file_key,
                permission_granted=True,
            )
            # TODO: await store_audit_log(audit_entry)
            
            # Download from cloud storage
            with file_handler.download(file_key) as f:
                content = f.read()
            
            return {
                "content": content,
                "filename": file_key.split("/")[-1],
            }
        
        except Exception as e:
            logger.error(f"Download failed: {e}")
            raise


# ============================================================================
# 8. HEALTH CHECK WITH OBSERVABILITY
# ============================================================================

@app.get("/health")
async def health():
    """Health check endpoint (observed by Prometheus)."""
    with tracer.start_as_current_span("health_check"):
        logger.info("Health check passed")
        return {"status": "healthy", "service": "mailsvc"}


# ============================================================================
# 9. METRICS ENDPOINT (for Prometheus)
# ============================================================================

@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint."""
    # OpenTelemetry automatically exports metrics here
    # This endpoint is scraped by Prometheus
    from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
    return generate_latest()


# ============================================================================
# 10. STARTUP HOOK
# ============================================================================

@app.on_event("startup")
async def startup_event():
    """Initialize services on startup."""
    logger.info("Mailsvc starting up")
    logger.info(f"Using file storage backend: {file_handler.backend.__class__.__name__}")
    logger.info(f"Registered services: {list(service_auth.services.keys())}")


if __name__ == "__main__":
    import uvicorn
    
    # Configuration via environment variables
    import os
    
    port = int(os.getenv("PORT", "8080"))
    
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
    )
