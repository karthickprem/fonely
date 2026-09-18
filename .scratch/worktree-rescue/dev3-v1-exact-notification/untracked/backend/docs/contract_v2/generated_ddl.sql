CREATE TABLE owner_command_proposals (
  id VARCHAR(36) PRIMARY KEY,
  business_id INTEGER NOT NULL REFERENCES businesses.id,
  owner_user_id INTEGER NOT NULL,
  invocation_id VARCHAR(36) NOT NULL,
  family_id VARCHAR(64) NOT NULL,
  attempt_number INTEGER NOT NULL DEFAULT 1 CHECK ( > 0),
  command_type VARCHAR(40) NOT NULL CHECK ( IN ('close_clinic', 'close_early', 'doctor_leave')),
  command_payload JSONB NOT NULL,
  preview_snapshot JSONB NOT NULL,
  payload_digest VARCHAR(64) NOT NULL,
  status VARCHAR(30) NOT NULL DEFAULT pending_confirmation CHECK ( IN ('pending_confirmation', 'rejected', 'expired', 'completed')),
  result_evidence JSONB,
  expected_version INTEGER NOT NULL DEFAULT 1 CHECK ( > 0),
  idempotency_key VARCHAR(200) NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  confirmed_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE owner_command_proposals ADD CONSTRAINT uq_invocation UNIQUE (business_id, invocation_id);
ALTER TABLE owner_command_proposals ADD CONSTRAINT uq_idempotency UNIQUE (business_id, idempotency_key);
ALTER TABLE owner_command_proposals ADD CONSTRAINT uq_family_attempt UNIQUE (business_id, family_id, attempt_number);
CREATE UNIQUE INDEX uq_owner_pending ON owner_command_proposals (business_id, owner_user_id) WHERE status = 'pending_confirmation';
ALTER TABLE owner_command_proposals ADD CONSTRAINT fk_business_user FOREIGN KEY (business_id, owner_user_id) REFERENCES (business_users.business_id, business_users.id);
CREATE INDEX ix_family ON owner_command_proposals (business_id, family_id);
CREATE INDEX ix_pending_expiry ON owner_command_proposals (status, expires_at) WHERE status = 'pending_confirmation';