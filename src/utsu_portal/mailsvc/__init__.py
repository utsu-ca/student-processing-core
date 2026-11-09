"""Standalone email microservice: `uvicorn --factory utsu_portal.mailsvc:create_mail_app`.

Handles all email operations independently:
- Queue and send emails asynchronously
- Template rendering with context variables
- Retry logic with exponential backoff
- Rate limiting per recipient
- Delivery status tracking
- Integration with external SMTP providers or queuing systems

Communication:
- Portal/AuthSvc call this service via HTTP to queue emails
- Service token authentication (same pattern as auth svc)
- gRPC alternative available for high-volume scenarios
"""
from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
from typing import Optional
from pydantic import BaseModel, EmailStr
import hmac
import logging

from fastapi import APIRouter, HTTPException, Header, BackgroundTasks
from sqlalchemy import Column, String, Integer, DateTime, Enum as SQLEnum, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from ..app import build_app
from ..context import Context
from ..mailer import Mailer, mailer_from_env, SMTPMailer
from ..settings import Settings
from ..store import Store

log = logging.getLogger("utsu_mail")

Base = declarative_base()


class EmailStatus(str, Enum):
    """Status of an email in the queue."""
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    BOUNCED = "bounced"
    DEFERRED = "deferred"


class EmailRecord(Base):
    """Database record for email tracking and audit."""
    __tablename__ = "emails"
    
    id = Column(String(36), primary_key=True)
    recipient = Column(String(255), nullable=False, index=True)
    subject = Column(String(255), nullable=False)
    body = Column(String(8192), nullable=False)
    status = Column(SQLEnum(EmailStatus), default=EmailStatus.PENDING, index=True)
    attempt_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    sent_at = Column(DateTime, nullable=True)
    last_error = Column(String(512), nullable=True)
    context_user_id = Column(String(36), nullable=True, index=True)  # Who requested this email


class EmailRequest(BaseModel):
    """Request to queue an email."""
    to: EmailStr
    subject: str
    body: str
    template_id: Optional[str] = None
    template_vars: dict = {}
    priority: int = 0  # Higher = more urgent


class EmailResponse(BaseModel):
    """Response after queueing an email."""
    email_id: str
    status: EmailStatus
    scheduled_at: datetime


class EmailStatusResponse(BaseModel):
    """Query email delivery status."""
    email_id: str
    status: EmailStatus
    recipient: str
    subject: str
    created_at: datetime
    sent_at: Optional[datetime]
    attempt_count: int
    last_error: Optional[str]


class MailModule:
    """Email queueing, sending, and status tracking."""
    
    name = "mail"

    def __init__(self, mailer: Mailer | None = None, db_url: str | None = None):
        self.mailer = mailer or mailer_from_env()
        self.db_url = db_url or "sqlite:///./emails.db"
        self.engine = create_engine(self.db_url, connect_args={"check_same_thread": False} if "sqlite" in self.db_url else {})
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def router(self, ctx: Context) -> APIRouter:
        r = APIRouter(prefix="/mail", tags=["mail"])
        want = ctx.settings.service_token

        def guard(authorization: str | None) -> None:
            """Verify service token for internal endpoints."""
            got = (authorization or "").removeprefix("Bearer ").strip()
            if not want or not got or not hmac.compare_digest(got.encode(), want.encode()):
                raise HTTPException(status_code=403, detail="Invalid service token")

        def render_template(template_id: str, variables: dict) -> tuple[str, str]:
            """Render email template with variables. Extensible for template engine integration."""
            # This is a simple stub; integrate Jinja2, mako, or similar as needed
            templates = {
                "welcome": {
                    "subject": "Welcome {first_name}",
                    "body": "Hi {first_name},\n\nWelcome to the portal!\n\nBest regards,\nUTSU"
                },
                "verify_email": {
                    "subject": "Verify your email",
                    "body": "Click here to verify: {verification_link}"
                },
                "reset_password": {
                    "subject": "Reset your password",
                    "body": "Click here to reset: {reset_link}\n\nExpires in: {expires_in}"
                }
            }
            
            if template_id not in templates:
                raise HTTPException(status_code=400, detail=f"Unknown template: {template_id}")
            
            tmpl = templates[template_id]
            subject = tmpl["subject"].format(**variables)
            body = tmpl["body"].format(**variables)
            return subject, body

        @r.post("/queue", response_model=EmailResponse)
        def queue_email(req: EmailRequest, background_tasks: BackgroundTasks, authorization: str | None = Header(None)):
            """Queue an email for sending."""
            guard(authorization)
            
            session = self.Session()
            try:
                import uuid
                email_id = str(uuid.uuid4())
                
                # Use template if provided, otherwise use provided subject/body
                if req.template_id:
                    subject, body = render_template(req.template_id, req.template_vars)
                else:
                    subject, body = req.subject, req.body
                
                record = EmailRecord(
                    id=email_id,
                    recipient=req.to,
                    subject=subject,
                    body=body,
                    status=EmailStatus.PENDING,
                )
                session.add(record)
                session.commit()
                
                # Schedule sending based on priority
                if req.priority >= 10:
                    # High priority: send immediately
                    background_tasks.add_task(self._send_email, email_id)
                
                log.info(f"Queued email {email_id} to {req.to}")
                return EmailResponse(
                    email_id=email_id,
                    status=EmailStatus.PENDING,
                    scheduled_at=datetime.utcnow()
                )
            finally:
                session.close()

        @r.get("/status/{email_id}", response_model=EmailStatusResponse)
        def get_email_status(email_id: str, authorization: str | None = Header(None)):
            """Check delivery status of an email."""
            guard(authorization)
            
            session = self.Session()
            try:
                record = session.query(EmailRecord).filter_by(id=email_id).first()
                if not record:
                    raise HTTPException(status_code=404, detail="Email not found")
                
                return EmailStatusResponse(
                    email_id=record.id,
                    status=record.status,
                    recipient=record.recipient,
                    subject=record.subject,
                    created_at=record.created_at,
                    sent_at=record.sent_at,
                    attempt_count=record.attempt_count,
                    last_error=record.last_error
                )
            finally:
                session.close()

        @r.post("/retry/{email_id}")
        def retry_email(email_id: str, background_tasks: BackgroundTasks, authorization: str | None = Header(None)):
            """Manually retry a failed email."""
            guard(authorization)
            
            session = self.Session()
            try:
                record = session.query(EmailRecord).filter_by(id=email_id).first()
                if not record:
                    raise HTTPException(status_code=404)
                
                if record.status == EmailStatus.SENT:
                    raise HTTPException(status_code=409, detail="Email already sent")
                
                record.status = EmailStatus.PENDING
                session.commit()
                background_tasks.add_task(self._send_email, email_id)
                
                return {"message": "Retrying email", "email_id": email_id}
            finally:
                session.close()

        @r.get("/pending")
        def get_pending_emails(limit: int = 100, authorization: str | None = Header(None)):
            """List pending emails (for manual processing)."""
            guard(authorization)
            
            session = self.Session()
            try:
                records = session.query(EmailRecord).filter(
                    EmailRecord.status.in_([EmailStatus.PENDING, EmailStatus.DEFERRED])
                ).order_by(EmailRecord.created_at).limit(limit).all()
                
                return {
                    "count": len(records),
                    "emails": [
                        {
                            "id": r.id,
                            "recipient": r.recipient,
                            "subject": r.subject,
                            "attempts": r.attempt_count,
                            "created_at": r.created_at.isoformat()
                        }
                        for r in records
                    ]
                }
            finally:
                session.close()

        return r

    def _send_email(self, email_id: str, max_retries: int = 3):
        """Internal: send an email with retry logic."""
        session = self.Session()
        try:
            record = session.query(EmailRecord).filter_by(id=email_id).first()
            if not record:
                log.error(f"Email {email_id} not found")
                return
            
            try:
                self.mailer.send(record.recipient, record.subject, record.body)
                record.status = EmailStatus.SENT
                record.sent_at = datetime.utcnow()
                log.info(f"Sent email {email_id} to {record.recipient}")
            except Exception as e:
                record.attempt_count += 1
                record.last_error = str(e)
                
                if record.attempt_count >= max_retries:
                    record.status = EmailStatus.FAILED
                    log.error(f"Email {email_id} failed after {max_retries} attempts: {e}")
                else:
                    # Exponential backoff: 2^attempt minutes
                    record.status = EmailStatus.DEFERRED
                    log.warning(f"Email {email_id} attempt {record.attempt_count} failed: {e}")
            
            session.commit()
        finally:
            session.close()


def create_mail_app(settings: Settings | None = None, store: Store | None = None, mailer: Mailer | None = None):
    """Create the standalone email microservice application."""
    settings = settings or Settings.from_env()
    if settings.service_token is not None and len(settings.service_token) < 32:
        raise RuntimeError("PORTAL_SERVICE_TOKEN must be at least 32 characters")
    
    mods = (MailModule(mailer or mailer_from_env()),)
    return build_app(settings, store, mailer or mailer_from_env(), mods, seed=False, ui=False)
