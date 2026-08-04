# Data Inventory

Tables containing PII or sensitive patient/business data.

## Patient-identifying data

| Table | PII Fields | Retention | Cleanup |
|---|---|---|---|
| appointments | customer_name, customer_phone | 365 days | Manual |
| appointment_commits | before/after snapshots (contain patient facts) | 365 days | Manual |
| conversations | customer_phone | 90 days | Automated |
| conversation_turns | user_message_hash (no text), assistant_response | 90 days (CASCADE) | Automated |
| notification_outbox | recipient_phone, recipient_name | 30 days (delivered), 90 days (dead letter) | Automated |
| whatsapp_inbound_events | sender_phone, temporary message_body | 30 days after completed/dead letter | Automated; body cleared immediately on completion |
| whatsapp_delivery_attempts | provider_message_id, sanitized error class | Follows notification lifecycle | Automated with notification evidence |
| pending_actions | proposed_payload (contains customer phone/name) | 90 days | Automated |
| calls | customer_phone | 365 days | Manual |

## Business data (not patient PII)

| Table | Fields | Retention | Cleanup |
|---|---|---|---|
| businesses | name, primary_contact_phone | Permanent | N/A |
| business_users | phone, role | Permanent | N/A |
| services | name, price, duration | Permanent | N/A |
| resources | name, type | Permanent | N/A |
| onboarding_drafts | business configuration | 365 days | Manual |

## Automated cleanup

The retention worker (`run_retention_worker.py`) runs every 6 hours and cleans:

1. **Conversations** in terminal states (completed/ended/escalated) older than 90 days, with CASCADE deletion of their turns.
2. **Notifications** that were delivered more than 30 days ago or dead-lettered more than 90 days ago.
3. **Pending actions** in terminal states (confirmed/rejected/expired) older than 90 days, only when not referenced by active appointments.
4. **WhatsApp inbound events** completed or dead-lettered more than 30 days ago. Successful processing clears `message_body` immediately; dead-letter bodies remain only for bounded operator investigation.

Active/in-progress records are never deleted regardless of age.

## Retention periods

Configurable via environment variables:

| Variable | Default | Description |
|---|---|---|
| `RETENTION_CONVERSATIONS_DAYS` | 90 | Completed conversations and turns |
| `RETENTION_APPOINTMENTS_DAYS` | 365 | Appointments and commits |
| `RETENTION_NOTIFICATIONS_DAYS` | 30 | Delivered notifications |
| `RETENTION_NOTIFICATIONS_DEAD_LETTER_DAYS` | 90 | Dead-lettered notifications |
| `RETENTION_WHATSAPP_INBOUND_DAYS` | 30 | Completed inbound WhatsApp events |
| `RETENTION_WHATSAPP_INBOUND_DEAD_LETTER_DAYS` | 30 | Dead-lettered inbound events with temporary message body |

## PII access logging

Every access to patient-identifying data is logged with:

- Operation type (read/search/export)
- Data type (appointment/conversation/phone)
- Business ID
- Accessor identity (api:internal, worker:notification, api:whatsapp)
- Record count
- Correlation ID

The actual PII values are never included in access logs.

## Patient data request

To export or delete a patient's data, an operator must:

1. Query all tables above by customer_phone.
2. Export relevant records.
3. Delete records respecting FK constraints.
4. This is a manual process until a self-service API is built.
