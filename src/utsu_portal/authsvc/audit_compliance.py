"""Audit compliance patterns for tracking reads/writes with access control.

This module provides database schema and access patterns to ensure audit compliance:
- Every read/write is tracked with: who (user/service), when, what, why
- Access control decisions are logged
- Data lineage is preserved
- Compliance reports can be generated

Key Patterns:
1. Audit Log Table: Immutable log of all data access
2. Access Control Table: Track permission checks
3. Data Lineage: Track data flow between services
4. Retention Policy: Data retention based on compliance requirements

All tables use PostgreSQL jsonb for flexible logging while maintaining queryability.
"""

from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from enum import Enum
from pydantic import BaseModel
import uuid


class AuditAction(str, Enum):
    """Types of actions that can be audited."""
    CREATE = "create"
    READ = "read"
    UPDATE = "update"
    DELETE = "delete"
    EXPORT = "export"
    DOWNLOAD = "download"
    UPLOAD = "upload"
    AUTHENTICATE = "authenticate"
    AUTHORIZE = "authorize"
    DENY = "deny"


class AuditLevel(str, Enum):
    """Severity/importance of audit events."""
    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    CRITICAL = "critical"


class AuditEntry(BaseModel):
    """Schema for audit log entries."""
    audit_id: str = None  # UUID
    timestamp: datetime = None
    
    # Who performed the action
    actor_id: str  # User ID, Service name, or System
    actor_type: str  # "user", "service", "system"
    actor_ip: Optional[str] = None
    
    # What resource was accessed
    resource_type: str  # "user", "file", "email", "audit_log", etc.
    resource_id: str  # ID of the resource (user_id, file_id, etc.)
    
    # The action performed
    action: AuditAction
    
    # Result and context
    result: str  # "success", "denied", "error"
    reason: Optional[str] = None  # Why the action was denied/failed
    
    # Permission/policy that was checked
    permission_required: Optional[str] = None
    permission_granted: bool = False
    
    # Additional context (JSON)
    context: Dict[str, Any] = None
    
    # For data tracking
    data_before: Optional[Dict[str, Any]] = None
    data_after: Optional[Dict[str, Any]] = None
    
    # Compliance metadata
    audit_level: AuditLevel = AuditLevel.INFO
    retention_days: int = 2555  # 7 years default
    
    def __post_init__(self):
        if not self.audit_id:
            self.audit_id = str(uuid.uuid4())
        if not self.timestamp:
            self.timestamp = datetime.utcnow()


# ============================================================================
# PostgreSQL Schema Definitions
# ============================================================================

AUDIT_LOG_TABLE_SQL = """
CREATE TABLE audit_log (
    audit_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    
    -- Actor information
    actor_id VARCHAR(255) NOT NULL,
    actor_type VARCHAR(50) NOT NULL,
    actor_ip INET,
    
    -- Resource information
    resource_type VARCHAR(100) NOT NULL,
    resource_id VARCHAR(255) NOT NULL,
    
    -- Action information
    action VARCHAR(50) NOT NULL,
    result VARCHAR(50) NOT NULL,
    reason TEXT,
    
    -- Permission/policy information
    permission_required VARCHAR(255),
    permission_granted BOOLEAN DEFAULT false,
    
    -- Context and data changes
    context JSONB,
    data_before JSONB,
    data_after JSONB,
    
    -- Compliance metadata
    audit_level VARCHAR(50) DEFAULT 'info',
    retention_days INT DEFAULT 2555,
    
    -- Full-text search index
    CONSTRAINT audit_log_data_consistency CHECK (
        CASE
            WHEN action IN ('update', 'delete') THEN data_before IS NOT NULL
            WHEN action IN ('create', 'upload') THEN data_after IS NOT NULL
            ELSE true
        END
    )
);

CREATE INDEX audit_log_timestamp_idx ON audit_log(timestamp DESC);
CREATE INDEX audit_log_actor_idx ON audit_log(actor_id, timestamp DESC);
CREATE INDEX audit_log_resource_idx ON audit_log(resource_type, resource_id, timestamp DESC);
CREATE INDEX audit_log_action_idx ON audit_log(action, timestamp DESC);
CREATE INDEX audit_log_result_idx ON audit_log(result, timestamp DESC);
"""

ACCESS_CONTROL_LOG_TABLE_SQL = """
CREATE TABLE access_control_log (
    check_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    
    -- Who is requesting access
    requester_id VARCHAR(255) NOT NULL,
    requester_type VARCHAR(50) NOT NULL,  -- "user", "service", "system"
    
    -- What resource/action they're requesting
    resource_type VARCHAR(100) NOT NULL,
    resource_id VARCHAR(255) NOT NULL,
    action_requested VARCHAR(50) NOT NULL,
    
    -- Decision details
    decision VARCHAR(50) NOT NULL,  -- "allow", "deny"
    policy_matched VARCHAR(255),  -- Which policy rule matched
    reason TEXT,
    
    -- RBAC/permission details
    roles JSONB,  -- Array of roles the user has
    permissions JSONB,  -- Array of permissions checked
    
    -- Request context
    request_path VARCHAR(500),
    request_method VARCHAR(10),
    request_ip INET,
    
    -- Timing
    decision_time_ms FLOAT  -- Milliseconds to make the decision
);

CREATE INDEX access_control_log_timestamp_idx ON access_control_log(timestamp DESC);
CREATE INDEX access_control_log_requester_idx ON access_control_log(requester_id, timestamp DESC);
CREATE INDEX access_control_log_decision_idx ON access_control_log(decision, timestamp DESC);
CREATE INDEX access_control_log_resource_idx ON access_control_log(resource_type, resource_id, timestamp DESC);
"""

DATA_LINEAGE_TABLE_SQL = """
CREATE TABLE data_lineage (
    lineage_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    
    -- Source data
    source_service VARCHAR(100) NOT NULL,
    source_resource_type VARCHAR(100) NOT NULL,
    source_resource_id VARCHAR(255) NOT NULL,
    
    -- Destination/derived data
    dest_service VARCHAR(100) NOT NULL,
    dest_resource_type VARCHAR(100) NOT NULL,
    dest_resource_id VARCHAR(255) NOT NULL,
    
    -- Transformation details
    operation VARCHAR(100) NOT NULL,  -- "copy", "transform", "aggregate", etc.
    transformation_rules JSONB,  -- How the data was transformed
    
    -- Who performed the operation
    actor_id VARCHAR(255) NOT NULL,
    actor_type VARCHAR(50) NOT NULL,
    
    -- Compliance metadata
    data_classification VARCHAR(50),  -- "public", "internal", "confidential", "restricted"
    consent_required BOOLEAN DEFAULT false,
    consent_given BOOLEAN DEFAULT false,
    pii_involved BOOLEAN DEFAULT false
);

CREATE INDEX data_lineage_source_idx ON data_lineage(source_service, source_resource_type, source_resource_id);
CREATE INDEX data_lineage_dest_idx ON data_lineage(dest_service, dest_resource_type, dest_resource_id);
CREATE INDEX data_lineage_timestamp_idx ON data_lineage(timestamp DESC);
"""

RETENTION_POLICY_TABLE_SQL = """
CREATE TABLE retention_policy (
    policy_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    resource_type VARCHAR(100) NOT NULL UNIQUE,
    
    -- Retention rules
    retention_days INT NOT NULL,
    hard_delete_days INT,  -- After this, permanently delete
    
    -- Anonymization rules
    anonymize_after_days INT,  -- Anonymize PII after this period
    anonymization_rules JSONB,  -- Which fields to anonymize
    
    -- Compliance info
    regulation VARCHAR(255),  -- "GDPR", "HIPAA", "SOC2", etc.
    reason TEXT,
    
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX retention_policy_regulation_idx ON retention_policy(regulation);
"""

COMPLIANCE_REPORT_TABLE_SQL = """
CREATE TABLE compliance_report (
    report_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    report_type VARCHAR(100) NOT NULL,  -- "access_summary", "data_export", "retention_check", etc.
    
    -- Report scope
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    resource_type VARCHAR(100),
    actor_id VARCHAR(255),
    
    -- Report data
    findings JSONB NOT NULL,  -- Details of what was found
    summary JSONB NOT NULL,  -- Statistics and summaries
    
    -- Report status
    status VARCHAR(50) DEFAULT 'generated',  -- "generated", "reviewed", "approved"
    reviewed_by VARCHAR(255),
    reviewed_at TIMESTAMPTZ,
    
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX compliance_report_date_idx ON compliance_report(start_date, end_date);
CREATE INDEX compliance_report_type_idx ON compliance_report(report_type);
"""

# ============================================================================
# SQL Queries for Compliance Operations
# ============================================================================

COMPLIANCE_QUERIES = {
    # Who accessed a specific resource?
    "who_accessed_resource": """
        SELECT
            actor_id,
            actor_type,
            action,
            timestamp,
            result,
            reason
        FROM audit_log
        WHERE resource_type = %s
          AND resource_id = %s
        ORDER BY timestamp DESC
        LIMIT %s;
    """,
    
    # What resources did a user access?
    "user_access_history": """
        SELECT
            resource_type,
            resource_id,
            action,
            timestamp,
            result,
            reason
        FROM audit_log
        WHERE actor_id = %s
          AND actor_type = 'user'
          AND timestamp >= %s
        ORDER BY timestamp DESC;
    """,
    
    # Were all permission checks applied?
    "permission_check_audit": """
        SELECT
            requester_id,
            resource_type,
            resource_id,
            decision,
            policy_matched,
            timestamp
        FROM access_control_log
        WHERE decision = 'deny'
          AND timestamp >= %s
        ORDER BY timestamp DESC;
    """,
    
    # Data export audit trail
    "data_export_history": """
        SELECT
            actor_id,
            resource_type,
            resource_id,
            timestamp,
            context ->> 'export_format' as format,
            context ->> 'export_destination' as destination
        FROM audit_log
        WHERE action = 'export'
          AND timestamp >= %s
        ORDER BY timestamp DESC;
    """,
    
    # Compliance check: are all PII transfers logged?
    "pii_transfer_audit": """
        SELECT
            *
        FROM data_lineage
        WHERE pii_involved = true
          AND timestamp >= %s
        ORDER BY timestamp DESC;
    """,
    
    # Find data with expired retention
    "expired_retention": """
        SELECT
            al.audit_id,
            al.resource_type,
            al.resource_id,
            al.timestamp,
            al.data_after,
            rp.retention_days,
            (al.timestamp + rp.retention_days * interval '1 day') as expiry_date
        FROM audit_log al
        JOIN retention_policy rp ON al.resource_type = rp.resource_type
        WHERE (al.timestamp + rp.retention_days * interval '1 day') < CURRENT_TIMESTAMP
        ORDER BY al.timestamp ASC;
    """,
}


# ============================================================================
# Audit Event Builders (for application code)
# ============================================================================

def build_audit_entry_read(
    actor_id: str,
    actor_type: str,
    resource_type: str,
    resource_id: str,
    permission_granted: bool,
    permission_required: Optional[str] = None,
    reason: Optional[str] = None,
    actor_ip: Optional[str] = None,
) -> AuditEntry:
    """Build an audit entry for a read operation."""
    return AuditEntry(
        actor_id=actor_id,
        actor_type=actor_type,
        actor_ip=actor_ip,
        resource_type=resource_type,
        resource_id=resource_id,
        action=AuditAction.READ,
        result="success" if permission_granted else "denied",
        permission_required=permission_required,
        permission_granted=permission_granted,
        reason=reason,
        audit_level=AuditLevel.WARN if not permission_granted else AuditLevel.INFO,
    )


def build_audit_entry_write(
    actor_id: str,
    actor_type: str,
    resource_type: str,
    resource_id: str,
    action: AuditAction,
    data_before: Optional[Dict] = None,
    data_after: Optional[Dict] = None,
    permission_granted: bool = True,
    permission_required: Optional[str] = None,
    reason: Optional[str] = None,
    actor_ip: Optional[str] = None,
) -> AuditEntry:
    """Build an audit entry for a write operation (create/update/delete)."""
    return AuditEntry(
        actor_id=actor_id,
        actor_type=actor_type,
        actor_ip=actor_ip,
        resource_type=resource_type,
        resource_id=resource_id,
        action=action,
        result="success" if permission_granted else "denied",
        data_before=data_before,
        data_after=data_after,
        permission_required=permission_required,
        permission_granted=permission_granted,
        reason=reason,
        audit_level=AuditLevel.WARN if not permission_granted else AuditLevel.INFO,
        # Writes should be retained longer for compliance
        retention_days=2555,  # 7 years
    )


def build_audit_entry_access_denied(
    actor_id: str,
    actor_type: str,
    resource_type: str,
    resource_id: str,
    permission_required: str,
    reason: str,
    actor_ip: Optional[str] = None,
) -> AuditEntry:
    """Build an audit entry for denied access."""
    return AuditEntry(
        actor_id=actor_id,
        actor_type=actor_type,
        actor_ip=actor_ip,
        resource_type=resource_type,
        resource_id=resource_id,
        action=AuditAction.AUTHORIZE,
        result="denied",
        permission_required=permission_required,
        permission_granted=False,
        reason=reason,
        audit_level=AuditLevel.WARN,
    )


__all__ = [
    "AuditEntry",
    "AuditAction",
    "AuditLevel",
    "AUDIT_LOG_TABLE_SQL",
    "ACCESS_CONTROL_LOG_TABLE_SQL",
    "DATA_LINEAGE_TABLE_SQL",
    "RETENTION_POLICY_TABLE_SQL",
    "COMPLIANCE_REPORT_TABLE_SQL",
    "COMPLIANCE_QUERIES",
    "build_audit_entry_read",
    "build_audit_entry_write",
    "build_audit_entry_access_denied",
]
