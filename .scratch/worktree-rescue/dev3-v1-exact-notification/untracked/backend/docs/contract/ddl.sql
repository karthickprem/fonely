-- Owner Command Proposals DDL (0015 final shape)

-- Prerequisite: composite unique on business_users
ALTER TABLE business_users
  ADD CONSTRAINT uq_business_users_business_id UNIQUE (business_id, id);

CREATE TABLE owner_command_proposals (
  id                  VARCHAR(36) PRIMARY KEY,
  business_id         INTEGER NOT NULL REFERENCES businesses(id),
  owner_user_id       INTEGER NOT NULL,
  invocation_id       VARCHAR(36) NOT NULL,
  family_id           VARCHAR(64) NOT NULL,
  attempt_number      INTEGER NOT NULL DEFAULT 1
                      CHECK (attempt_number > 0),
  command_type        VARCHAR(40) NOT NULL
                      CHECK (command_type IN ('close_clinic', 'close_early', 'doctor_leave')),
  command_payload     JSONB NOT NULL,
  preview_snapshot    JSONB NOT NULL,
  payload_digest      VARCHAR(64) NOT NULL,
  status              VARCHAR(30) NOT NULL DEFAULT 'pending_confirmation'
                      CHECK (status IN ('pending_confirmation', 'executing',
                             'completed', 'rejected', 'expired', 'failed')),
  result_evidence     JSONB,
  expected_version    INTEGER NOT NULL DEFAULT 1
                      CHECK (expected_version > 0),
  idempotency_key     VARCHAR(200) NOT NULL,
  expires_at          TIMESTAMPTZ NOT NULL,
  confirmed_at        TIMESTAMPTZ,
  completed_at        TIMESTAMPTZ,
  failure_code        VARCHAR(100),
  failure_message     VARCHAR(500),
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT fk_owner_proposal_business_user
    FOREIGN KEY (business_id, owner_user_id)
    REFERENCES business_users(business_id, id)
);

-- Transport dedup
ALTER TABLE owner_command_proposals
  ADD CONSTRAINT uq_owner_proposal_invocation
  UNIQUE (business_id, invocation_id);

-- Semantic dedup
ALTER TABLE owner_command_proposals
  ADD CONSTRAINT uq_owner_proposal_idempotency
  UNIQUE (business_id, idempotency_key);

-- Locked attempt allocation
ALTER TABLE owner_command_proposals
  ADD CONSTRAINT uq_owner_proposal_family_attempt
  UNIQUE (business_id, family_id, attempt_number);

-- At most one active per owner (pending OR executing)
CREATE UNIQUE INDEX uq_owner_proposal_owner_active
  ON owner_command_proposals (business_id, owner_user_id)
  WHERE status IN ('pending_confirmation', 'executing');

-- Family queries
CREATE INDEX ix_owner_proposal_family
  ON owner_command_proposals (business_id, family_id);

-- Expiry sweeper
CREATE INDEX ix_owner_proposal_pending_expiry
  ON owner_command_proposals (status, expires_at)
  WHERE status = 'pending_confirmation';

-- Downgrade guard
-- DO $$ BEGIN
--   IF EXISTS (SELECT 1 FROM owner_command_proposals) THEN
--     RAISE EXCEPTION '0015 downgrade blocked: rows exist';
--   END IF;
-- END $$;
