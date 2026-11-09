-- PostgreSQL initialization script for audit compliance database
-- This creates the audit logging tables

CREATE SCHEMA IF NOT EXISTS audit;

-- ============================================================================
-- AUDIT LOG TABLE - Core compliance record
-- ============================================================================

CREATE TABLE audit.audit_log (
    audit_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    
    -- Actor information
    actor_id VARCHAR(255) NOT NULL,
    actor_type VARCHAR(50) NOT NULL CHECK (actor_type IN ('user', 'service', 'system')),
    actor_ip INET,
    
    -- Resource information
    resource_type VARCHAR(100) NOT NULL,
    resource_id VARCHAR(255) NOT NULL,
    
    -- Action information
    action VARCHAR(50) NOT NULL CHECK (action IN ('create', 'read', 'update', 'delete', 'export', 'download', 'upload', 'authenticate', 'authorize', 'deny')),
    result VARCHAR(50) NOT NULL CHECK (result IN ('success', 'denied', 'error')),
    reason TEXT,
    
    -- Permission/policy information
    permission_required VARCHAR(255),
    permission_granted BOOLEAN DEFAULT false,
    
    -- Context and data changes
    context JSONB,
    data_before JSONB,
    data_after JSONB,
    
    -- Compliance metadata
    audit_level VARCHAR(50) DEFAULT 'info' CHECK (audit_level IN ('info', 'warn', 'error', 'critical')),
    retention_days INT DEFAULT 2555,
    
    -- Data consistency validation
    CONSTRAINT audit_log_data_consistency CHECK (
        CASE
            WHEN action IN ('update', 'delete') THEN data_before IS NOT NULL
            WHEN action IN ('create', 'upload') THEN data_after IS NOT NULL
            ELSE true
        END
    )
);

CREATE INDEX audit_log_timestamp_idx ON audit.audit_log(timestamp DESC);
CREATE INDEX audit_log_actor_idx ON audit.audit_log(actor_id, timestamp DESC);
CREATE INDEX audit_log_resource_idx ON audit.audit_log(resource_type, resource_id, timestamp DESC);
CREATE INDEX audit_log_action_idx ON audit.audit_log(action, timestamp DESC);
CREATE INDEX audit_log_result_idx ON audit.audit_log(result, timestamp DESC);
CREATE INDEX audit_log_data_before_idx ON audit.audit_log USING GIN(data_before);
CREATE INDEX audit_log_data_after_idx ON audit.audit_log USING GIN(data_after);

-- ============================================================================
-- ACCESS CONTROL LOG TABLE - Permission decisions
-- ============================================================================

CREATE TABLE audit.access_control_log (
    check_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    
    -- Who is requesting access
    requester_id VARCHAR(255) NOT NULL,
    requester_type VARCHAR(50) NOT NULL CHECK (requester_type IN ('user', 'service', 'system')),
    
    -- What resource/action they're requesting
    resource_type VARCHAR(100) NOT NULL,
    resource_id VARCHAR(255) NOT NULL,
    action_requested VARCHAR(50) NOT NULL,
    
    -- Decision details
    decision VARCHAR(50) NOT NULL CHECK (decision IN ('allow', 'deny')),
    policy_matched VARCHAR(255),
    reason TEXT,
    
    -- RBAC/permission details
    roles JSONB,  -- Array of roles
    permissions JSONB,  -- Array of permissions
    
    -- Request context
    request_path VARCHAR(500),
    request_method VARCHAR(10),
    request_ip INET,
    
    -- Timing
    decision_time_ms FLOAT
);

CREATE INDEX access_control_log_timestamp_idx ON audit.access_control_log(timestamp DESC);
CREATE INDEX access_control_log_requester_idx ON audit.access_control_log(requester_id, timestamp DESC);
CREATE INDEX access_control_log_decision_idx ON audit.access_control_log(decision, timestamp DESC);
CREATE INDEX access_control_log_resource_idx ON audit.access_control_log(resource_type, resource_id, timestamp DESC);

-- ============================================================================
-- DATA LINEAGE TABLE - Track data flow between services
-- ============================================================================

CREATE TABLE audit.data_lineage (
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
    operation VARCHAR(100) NOT NULL,
    transformation_rules JSONB,
    
    -- Who performed the operation
    actor_id VARCHAR(255) NOT NULL,
    actor_type VARCHAR(50) NOT NULL,
    
    -- Compliance metadata
    data_classification VARCHAR(50) CHECK (data_classification IN ('public', 'internal', 'confidential', 'restricted')),
    consent_required BOOLEAN DEFAULT false,
    consent_given BOOLEAN DEFAULT false,
    pii_involved BOOLEAN DEFAULT false
);

CREATE INDEX data_lineage_source_idx ON audit.data_lineage(source_service, source_resource_type, source_resource_id);
CREATE INDEX data_lineage_dest_idx ON audit.data_lineage(dest_service, dest_resource_type, dest_resource_id);
CREATE INDEX data_lineage_timestamp_idx ON audit.data_lineage(timestamp DESC);
CREATE INDEX data_lineage_pii_idx ON audit.data_lineage(pii_involved, timestamp DESC);

-- ============================================================================
-- RETENTION POLICY TABLE - Compliance rules
-- ============================================================================

CREATE TABLE audit.retention_policy (
    policy_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    resource_type VARCHAR(100) NOT NULL UNIQUE,
    
    -- Retention rules
    retention_days INT NOT NULL,
    hard_delete_days INT,
    
    -- Anonymization rules
    anonymize_after_days INT,
    anonymization_rules JSONB,
    
    -- Compliance info
    regulation VARCHAR(255),
    reason TEXT,
    
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX retention_policy_regulation_idx ON audit.retention_policy(regulation);

-- Insert default retention policies
INSERT INTO audit.retention_policy (resource_type, retention_days, hard_delete_days, anonymize_after_days, regulation, reason) VALUES
('user', 2555, 3650, 365, 'HIPAA', 'HIPAA requires 7-year retention of medical records'),
('file', 1825, 2555, 365, 'SOC2', 'SOC2 compliance requires retention of audit logs'),
('email', 90, 180, 30, 'GDPR', 'GDPR allows 90-day retention for transactional email'),
('audit_log', 2555, 3650, NULL, 'HIPAA', 'Audit logs must be retained for 7 years'),
('consent', 2555, 3650, NULL, 'GDPR', 'Consent records must be retained for 7 years');

-- ============================================================================
-- COMPLIANCE REPORT TABLE - Generated reports
-- ============================================================================

CREATE TABLE audit.compliance_report (
    report_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    report_type VARCHAR(100) NOT NULL,
    
    -- Report scope
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    resource_type VARCHAR(100),
    actor_id VARCHAR(255),
    
    -- Report data
    findings JSONB NOT NULL,
    summary JSONB NOT NULL,
    
    -- Report status
    status VARCHAR(50) DEFAULT 'generated' CHECK (status IN ('generated', 'reviewed', 'approved')),
    reviewed_by VARCHAR(255),
    reviewed_at TIMESTAMPTZ,
    
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX compliance_report_date_idx ON audit.compliance_report(start_date, end_date);
CREATE INDEX compliance_report_type_idx ON audit.compliance_report(report_type);

-- ============================================================================
-- ROLES AND PERMISSIONS TABLE
-- ============================================================================

CREATE TABLE audit.service_roles (
    role_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    service_name VARCHAR(100) NOT NULL,
    role_name VARCHAR(100) NOT NULL,
    permissions JSONB NOT NULL,  -- Array of permission strings
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(service_name, role_name)
);

-- ============================================================================
-- FUNCTIONS FOR COMPLIANCE QUERIES
-- ============================================================================

-- Function: Get audit history for a resource
CREATE OR REPLACE FUNCTION audit.get_resource_history(
    p_resource_type VARCHAR,
    p_resource_id VARCHAR,
    p_days INT DEFAULT 90
)
RETURNS TABLE (
    audit_id UUID,
    timestamp TIMESTAMPTZ,
    actor_id VARCHAR,
    actor_type VARCHAR,
    action VARCHAR,
    result VARCHAR,
    reason TEXT
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        al.audit_id,
        al.timestamp,
        al.actor_id,
        al.actor_type,
        al.action,
        al.result,
        al.reason
    FROM audit.audit_log al
    WHERE al.resource_type = p_resource_type
      AND al.resource_id = p_resource_id
      AND al.timestamp > CURRENT_TIMESTAMP - (p_days || ' days')::INTERVAL
    ORDER BY al.timestamp DESC;
END;
$$ LANGUAGE plpgsql;

-- Function: Check if access was denied
CREATE OR REPLACE FUNCTION audit.get_denied_access(
    p_days INT DEFAULT 7
)
RETURNS TABLE (
    actor_id VARCHAR,
    resource_type VARCHAR,
    resource_id VARCHAR,
    timestamp TIMESTAMPTZ,
    reason TEXT
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        al.actor_id,
        al.resource_type,
        al.resource_id,
        al.timestamp,
        al.reason
    FROM audit.audit_log al
    WHERE al.result = 'denied'
      AND al.timestamp > CURRENT_TIMESTAMP - (p_days || ' days')::INTERVAL
    ORDER BY al.timestamp DESC;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- GRANTS - Restrict access to audit tables
-- ============================================================================

-- Only audit_service user can write to audit logs
GRANT SELECT ON audit.audit_log TO audit_service;
GRANT INSERT ON audit.audit_log TO audit_service;
GRANT SELECT ON audit.access_control_log TO audit_service;
GRANT INSERT ON audit.access_control_log TO audit_service;
GRANT SELECT ON audit.data_lineage TO audit_service;
GRANT INSERT ON audit.data_lineage TO audit_service;
GRANT SELECT ON audit.retention_policy TO audit_service;
GRANT SELECT ON audit.compliance_report TO audit_service;

-- ============================================================================
-- SAMPLE DATA - For testing
-- ============================================================================

-- Insert a sample audit log entry
INSERT INTO audit.audit_log (
    actor_id, actor_type, resource_type, resource_id,
    action, result, permission_required, permission_granted,
    data_after, context
) VALUES (
    'user-123', 'user',
    'email', 'email-456',
    'create', 'success',
    'send:email', true,
    '{"recipient": "student@utsu.ca", "subject": "Welcome"}'::JSONB,
    '{"ip": "192.168.1.1", "user_agent": "Mozilla/5.0"}'::JSONB
);

-- Insert a sample access denial
INSERT INTO audit.access_control_log (
    requester_id, requester_type, resource_type, resource_id, action_requested,
    decision, reason, permissions
) VALUES (
    'user-789', 'user',
    'file', 'file-secret',
    'read',
    'deny', 'Insufficient role',
    '["file:read:public"]'::JSONB
);

COMMIT;
