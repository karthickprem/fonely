# Exotel Deployment, Acceptance, and Post-0015 Plan

Status: DESIGNED and UNIT-TESTED. NOT production-ready.
  - Route: ABSENT from production app (intentionally not mounted)
  - Worker: NON-RUNNABLE (requires provider_call_sid migration)
  - Schema: NOT MIGRATED (waiting Dev3 0015 integration)
  - Intake: NOT WIRED to production session factory
  - Gateway: IP ranges UNKNOWN (requires sandbox or Exotel support)
  - Fixtures: SYNTHETIC (requires sandbox capture for verification)
Owner: Dev1.

---

## 1. Callback Provisioning Checklist

Pre-requisites before enabling the Exotel callback route:

### Exotel Dashboard Configuration
- [ ] Identify the Exotel virtual number(s) assigned to each business
- [ ] For each virtual number, configure StatusCallback URL:
      `https://<gateway-host>/webhooks/exotel/call-status`
- [ ] Set StatusCallbackEvents: `terminal,answered`
- [ ] Set StatusCallbackContentType: `application/json`
      (If JSON is not available, multipart/form-data is also accepted)
- [ ] Set CustomField to `<business_id>:<correlation_nonce>` for outbound calls
- [ ] Record the virtual number → business_id mapping for `EXOTEL_NUMBER_MAPPINGS`

### Fonely Environment Configuration
- [ ] Generate high-entropy secret: `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`
      Must be >= 32 ASCII chars, no surrounding whitespace
- [ ] Set `EXOTEL_WEBHOOK_SECRET` in secret manager
- [ ] Set `EXOTEL_NUMBER_MAPPINGS` as JSON: `{"08012345678": 1, "08087654321": 2}`
- [ ] Verify `/health/ready` returns 200 (weak secret → route not mounted → 503)

### Gateway/Reverse Proxy Configuration
- [ ] Configure source IP restriction (see §3 below)
- [ ] Inject `X-Exotel-Webhook-Secret` header with the configured value
- [ ] Forward `Content-Type` header unchanged
- [ ] Preserve request body without re-encoding
- [ ] Set upstream timeout >= 10s

### Verification
- [ ] Send test callback via curl with correct secret → 200
- [ ] Send test callback without secret → 401
- [ ] Send test callback with weak/wrong secret → 401
- [ ] Verify no phone numbers in application logs
- [ ] Verify call event persisted in exotel_inbound_events table

---

## 2. Sanitized Fixture Capture Procedure

### Purpose
Replace synthetic fixtures (backend/tests/fixtures/exotel_callbacks/) with
real Exotel sandbox-captured payloads to verify OQ-1 through OQ-8.

### Capture Tool

```bash
#!/bin/bash
# exotel-fixture-capture.sh — run behind the gateway during sandbox testing
# Captures raw callback payloads to sanitized fixture files.

CAPTURE_DIR="$(dirname "$0")/captured"
mkdir -p "$CAPTURE_DIR"

# Start a temporary capture server on the callback URL
python3 -c "
import json, hashlib, sys, os
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

CAPTURE_DIR = '$CAPTURE_DIR'
COUNTER = [0]

class CaptureHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        ct = self.headers.get('Content-Type', '')

        COUNTER[0] += 1
        timestamp = datetime.utcnow().strftime('%Y%m%dT%H%M%S')
        filename = f'{timestamp}_{COUNTER[0]:03d}.json'

        # Parse and sanitize
        if 'json' in ct:
            data = json.loads(body)
        else:
            # multipart: log raw for manual extraction
            with open(os.path.join(CAPTURE_DIR, f'{filename}.raw'), 'wb') as f:
                f.write(body)
            data = {'_raw_content_type': ct, '_note': 'multipart — extract manually'}

        # Sanitize: replace real phone numbers with fictional ones
        sanitized = {}
        PHONE_MAP = {}
        phone_counter = [0]
        def sanitize_phone(phone):
            if phone not in PHONE_MAP:
                phone_counter[0] += 1
                PHONE_MAP[phone] = f'+91900000{phone_counter[0]:04d}'
            return PHONE_MAP[phone]

        for key, value in data.items():
            if key in ('From', 'To') and isinstance(value, str) and len(value) > 5:
                sanitized[key] = sanitize_phone(value)
            else:
                sanitized[key] = value

        sanitized['_capture_metadata'] = {
            'source': 'sandbox',
            'captured_at': timestamp,
            'content_type': ct,
            'original_fields': list(data.keys()),
        }

        with open(os.path.join(CAPTURE_DIR, filename), 'w') as f:
            json.dump(sanitized, f, indent=2)

        print(f'Captured: {filename} ({len(data)} fields)')
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'ok')

HTTPServer(('0.0.0.0', 8080), CaptureHandler).serve_forever()
"
```

### Capture Scenarios
1. Initiate outbound call with StatusCallback + JSON content type
2. Wait for `answered` callback — capture
3. Wait for `completed` callback — capture
4. Initiate call to busy number — capture `busy` callback
5. Initiate call to unreachable number — capture `failed` callback
6. Let call ring without answer — capture `no-answer` callback
7. Repeat scenario 1 WITHOUT StatusCallbackContentType to observe default
8. Compare captured field names against OQ-1 expectations

### Sanitization Rules
- Replace all phone numbers with fictional `+91900000XXXX` series
- Replace Exotel account SID with `EXOTEL_SID_REDACTED`
- Preserve CallSid format but replace value with synthetic hex
- Preserve all other field names and structure exactly
- Record original field count and content type for parity verification

### Post-Capture Actions
- [ ] Update synthetic fixtures to match captured field names
- [ ] Verify parser handles all captured fields without rejection
- [ ] Document any undocumented fields discovered
- [ ] Update EXOTEL_PROVIDER_CONTRACT.md §14 with sandbox-verified status

---

## 3. Gateway/IP Allowlist Deployment Design

### Architecture

```
  Exotel servers (IP ranges TBD)
       │
       ▼
  ┌─────────────────────────┐
  │  Reverse Proxy / CDN    │  ← IP allowlist enforced here
  │  (nginx / Cloudflare /  │  ← Injects X-Exotel-Webhook-Secret
  │   AWS ALB / GCP LB)     │  ← Rate limits per source IP
  └────────────┬────────────┘
               │
               ▼
  ┌─────────────────────────┐
  │  Fonely API             │  ← Validates secret (defense-in-depth)
  │  /webhooks/exotel/      │  ← Validates content, parses, persists
  │  call-status            │
  └─────────────────────────┘
```

### IP Range Discovery (requires sandbox or Exotel support)
- Option A: Capture source IPs during sandbox fixture testing
- Option B: File Exotel support ticket requesting callback source IP ranges
- Option C: Use ASN-based lookup for Exotel's network blocks

### nginx Configuration Template

```nginx
# /etc/nginx/conf.d/exotel-webhook.conf
upstream fonely_api {
    server 127.0.0.1:8000;
}

server {
    listen 443 ssl;
    server_name webhooks.fonely.example.com;

    # Exotel callback source IPs (replace with actual ranges)
    # OQ-4: These MUST be verified via sandbox or Exotel support
    set $exotel_allowed 0;
    # Example ranges — NOT production values:
    # if ($remote_addr ~ "^103\.21\.") { set $exotel_allowed 1; }
    # if ($remote_addr ~ "^52\.66\.") { set $exotel_allowed 1; }

    location /webhooks/exotel/ {
        # Uncomment after IP ranges are verified:
        # if ($exotel_allowed = 0) { return 403; }

        # Inject the shared secret
        proxy_set_header X-Exotel-Webhook-Secret $EXOTEL_WEBHOOK_SECRET;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Real-IP $remote_addr;

        # Size limit (matches adapter's 64 KiB)
        client_max_body_size 64k;

        proxy_pass http://fonely_api;
        proxy_connect_timeout 5s;
        proxy_read_timeout 15s;
    }
}
```

### Deployment Steps
1. Deploy nginx config with IP restriction COMMENTED OUT
2. Test with sandbox callbacks through the proxy
3. Capture and record source IPs from sandbox
4. Uncomment IP restriction with verified ranges
5. Test again — verify callbacks still succeed
6. Monitor for any callbacks from unexpected IPs

---

## 4. Migration and Worker Acceptance Matrix

### Migration Prerequisites
| ID | Prerequisite | Status |
|----|-------------|--------|
| M-1 | Dev3 0015 integrated into main | BLOCKED (waiting Dev3) |
| M-2 | Alembic head known | BLOCKED (depends on M-1) |
| M-3 | Schema reviewed and matched to design doc | READY (EXOTEL_MIGRATION_WORKER_DESIGN.md) |
| M-4 | calls.provider_call_sid column designed | READY |
| M-5 | Downgrade safety verified | READY (table drop only) |

### Migration Acceptance Tests
| ID | Test | Type | Expected |
|----|------|------|----------|
| MA-1 | Upgrade creates exotel_inbound_events table | PG | Table exists with all columns |
| MA-2 | Unique constraint on (business_id, call_sid, event_type) | PG | Duplicate INSERT rejected |
| MA-3 | Check constraints on intake_status valid | PG | Invalid status rejected |
| MA-4 | Check constraint on attempts bounded | PG | attempts > max_attempts rejected |
| MA-5 | Claim consistency constraint | PG | processing without claim_token rejected |
| MA-6 | calls.provider_call_sid column exists | PG | Nullable, unique index with business_id |
| MA-7 | Downgrade drops table cleanly | PG | No FK dependencies |
| MA-8 | ORM parity with schema | Unit | All columns match |
| MA-9 | Index on (intake_status, next_attempt_at) exists | PG | Poll query uses index |

### Worker Acceptance Tests
| ID | Test | Type | Expected |
|----|------|------|----------|
| WA-1 | Claim → process → complete lifecycle | PG | Event status: received → processing → completed |
| WA-2 | Claim → fail → retry → complete | PG | Backoff applied, re-claimable |
| WA-3 | Max attempts → dead letter | PG | Status: dead_letter, not reclaimable |
| WA-4 | Stale claim token rejected | PG | mark_completed returns False |
| WA-5 | Expired lease reclaimable | PG | New claim succeeds after lease expires |
| WA-6 | Advisory lock serializes per CallSid | PG independent-session | Blocker observed |
| WA-7 | Domain mutation creates call record | PG | calls row with provider_call_sid |
| WA-8 | Forward-only transition enforced | PG | Terminal → non-terminal raises |
| WA-9 | False fenced completion → rollback | PG | No domain mutation persisted |
| WA-10 | Concurrent workers on different CallSids | PG | Both succeed independently |

---

## 5. Operator Runbook

### Monitoring
| Metric | Alert threshold | Action |
|--------|----------------|--------|
| exotel_callback_processed rate | Drop > 90% for 5 min | Check Exotel dashboard for outage |
| exotel_callback_rejected rate | > 10 per minute | Check secret rotation, gateway config |
| exotel_callback_duplicate rate | Sustained > 50% | Check Exotel retry behavior |
| exotel_callback_conflict rate | Any | Investigate provider anomaly |
| exotel_callback_late_noop rate | Sustained > 20% | Log-level anomaly, investigate ordering |
| intake_status = dead_letter count | Any new | Investigate processing failures |
| worker claim age > 10 min | Any | Check worker health, lease expiry |

### Incident Response
| Symptom | Diagnosis | Resolution |
|---------|-----------|------------|
| All callbacks 401 | Secret mismatch | Verify secret in config and gateway |
| All callbacks 404 | Number mapping missing | Update EXOTEL_NUMBER_MAPPINGS |
| All callbacks 503 | Intake not configured or weak secret | Check EXOTEL_WEBHOOK_SECRET strength |
| Callbacks 409 | Conflicting terminal events | Provider anomaly — log and investigate |
| Worker not processing | Dead worker or no eligible events | Check worker logs, intake_status distribution |
| Growing dead_letter count | Persistent processing failure | Check domain mutation errors, data issues |
| Duplicate rate high | Exotel retrying on timeout | Increase gateway timeout or check app latency |

### Secret Rotation
1. Generate new secret (>= 32 random ASCII chars)
2. Update secret manager
3. Update gateway header injection configuration
4. Rolling restart API instances
5. Verify callbacks succeed with new secret
6. Remove old secret from all systems
7. Verify `/health/ready` returns 200

---

## 6. Exact Post-0015 Rebase Plan

### Trigger
Dev3's 0015 migration is integrated into authoritative main and the
new Alembic head is known.

### Steps
1. `git fetch origin main`
2. `git rebase origin/main` on `worktree-dev1-exotel-provider-contract`
3. Resolve any conflicts in:
   - `backend/src/fonely/app.py` (if Dev3 modifies create_app)
   - `backend/src/fonely/models/schema.py` (if Dev3 adds models)
   - `backend/tests/integration/postgres/conftest.py` (if cleanup list changes)
4. Create migration revision at new head:
   - `alembic revision --autogenerate -m "exotel_inbound_events_and_call_identity"`
   - Or manual migration matching EXOTEL_MIGRATION_WORKER_DESIGN.md schema
5. Verify:
   - `alembic upgrade head` succeeds
   - `alembic check` shows no drift
   - Migration parity tests pass
   - All 91 existing unit tests still pass
6. Add PG integration tests from acceptance matrix (MA-1 through MA-9)
7. Add worker PG tests from acceptance matrix (WA-1 through WA-10)
8. Run full CI at exact SHA
9. Commit and report integration-ready candidate

### Expected Conflicts
- `app.py`: Dev3 may add notification routes; our Exotel gate is independent
- `conftest.py`: TRUNCATE list needs `exotel_inbound_events` added
- `schema.py`: Our new model definition must follow Dev3's additions
- No conflict expected in domain/calls/, repositories/exotel_intake.py,
  workers/exotel_worker.py, or tests/unit/channels/

### Timeline
- Rebase: < 1 hour after Dev3 integration
- Migration + PG tests: < 1 day
- Full CI: depends on runner availability
