# Public deployment — making a real phone call reach Fonely

`docs/STAGING_DEPLOYMENT.md` brings the stack up on `localhost`. That is enough
to run tests and enough for a developer, and it is not enough for the product:
a patient dials a number on a clinic signboard, Exotel receives that call, and
Exotel then has to reach *us* over the public internet. So does WhatsApp when
the clinic owner sends a message.

This document covers only that gap — the public edge. Everything else stays as
`docs/STAGING_DEPLOYMENT.md` describes it.

## What the edge does

`deploy/Caddyfile` terminates TLS and forwards a fixed allowlist to the backend:

| Public path | Purpose |
| --- | --- |
| `/webhooks/whatsapp` | Owner conversation — GET verification, POST deliveries |
| `/webhooks/exotel/call-status` | Call lifecycle callbacks |
| `/webhooks/exotel/audio-stream` | Patient audio, WebSocket, no timeout |
| `/health/live` | Liveness for an uptime monitor |

Every other path returns 404 from the edge.

That exclusion is deliberate and load-bearing. On `main` today `/metrics` and
`/health/alerts` answer **200 with no authentication** — verified by booting the
app and requesting them. `/metrics` exposes operational counters and
`/health/alerts` exposes internal failure state. Neither is anything a clinic's
competitor, or anyone else, should be able to poll. Until those endpoints are
authenticated in the application, the edge allowlist is the control that keeps
them private. Reach them over the private network or an SSH tunnel:

```bash
ssh -L 8000:127.0.0.1:8000 <host>
curl http://127.0.0.1:8000/metrics
```

## Host requirements

- A host with a public IPv4 address and ports 80 and 443 open inbound. Port 80
  is required — certificate issuance uses an inbound HTTP-01 challenge.
- A DNS A record pointing at that address, resolving **before** first start.
- Docker and Docker Compose.

A laptop, a NAT'd office machine, and this development box all fail the first
requirement. Use a small cloud VM in an Indian region — latency to the caller
is part of whether the conversation feels human.

## Configure

Add to `.env.staging`, on top of the variables the staging template already lists:

```
FONELY_PUBLIC_DOMAIN=api.example.in
FONELY_ACME_EMAIL=ops@example.in
EXOTEL_WEBHOOK_SECRET=<generated secret>
```

There is deliberately no number-to-clinic variable here. Which clinic a
dialled number reaches is tenant data, not process configuration, and since
migration 0017 it is a row in `business_channel_identities`. Attach a number
by API rather than by redeploy:

The public edge intentionally returns 404 for `/internal/*`. Register through
loopback on the host, or open an SSH tunnel from an operator machine:

```bash
ssh -L 8000:127.0.0.1:8000 <host>

curl -sS -X POST http://127.0.0.1:8000/internal/v1/businesses/channel-identity \
  -H "Authorization: Bearer $INTERNAL_API_SECRET" \
  -H "X-Business-ID: 1" \
  -H "Content-Type: application/json" \
  -d '{"provider": "exotel", "external_identifier": "+918000000000"}'
```

That binding is what an inbound call is resolved against — from the *dialled*
number, which the carrier controls, never from anything in the request body,
which a caller can forge. An unregistered number is refused with 404 at the
ringing webhook. Because the audio stream is admitted only against a `calls`
row that webhook wrote, a refused ringing also refuses the media stream: the
failure mode is a clinic whose calls do not connect, never a patient booked
into someone else's diary. Registering a number already held by another
tenant returns 409 instead of silently re-pointing a live line.

Mounting is conditional on configuration: `create_app()` includes the Exotel
router only when `EXOTEL_WEBHOOK_SECRET` is set, and the WhatsApp router only
when `WHATSAPP_VERIFY_TOKEN` is set. A missing variable does not fail loudly —
it silently produces a server with no telephony. Run the validator below
instead of trusting the container to complain.

## Start

```bash
docker compose \
  -f docker-compose.staging.yml \
  -f docker-compose.public.yml \
  --env-file .env.staging up -d
```

Confirm the edge is live and the private surfaces are not:

```bash
curl https://$FONELY_PUBLIC_DOMAIN/health/live     # → {"status":"ok"}
curl -o /dev/null -w '%{http_code}\n' https://$FONELY_PUBLIC_DOMAIN/metrics   # → 404
```

## Register with the providers

**WhatsApp Cloud API** — callback URL `https://<domain>/webhooks/whatsapp`,
verify token equal to `WHATSAPP_VERIFY_TOKEN`. Meta issues a GET with
`hub.mode=subscribe` and expects the challenge echoed back. Subscribe to the
`messages` field.

**Exotel** — status callback `https://<domain>/webhooks/exotel/call-status`,
audio stream `wss://<domain>/webhooks/exotel/audio-stream`. The stream is
`wss://`, not `https://`; the applet will not connect otherwise. Streaming is
not on by default on an Exotel account and has to be enabled for the number.

## Verify before pointing a real phone at it

Select every capability this host is expected to serve. A selected capability
with a missing or placeholder gate is a failure, not a warning:

```bash
python3 scripts/check-public-edge.py \
  --env-file .env.staging \
  --require whatsapp \
  --require exotel \
  --require internal
```

The validator checks selected router gates, rejects documentation placeholders,
rejects obsolete environment number mappings, verifies the Caddy allowlist, and
checks that the audio stream has no finite proxy timeout or buffering. Database-
backed channel identities are reported as **NOT RUN** because this static tool
deliberately has no database credentials. Verify those rows privately before the
first call. It does not import the application or dial a phone; only a mounted
host check and a real call prove those paths work.

The forwarded paths were checked against a booted application on `cc3aa65`:
`/health/live` → 200, `/webhooks/whatsapp` → 403 without a valid token,
`/webhooks/exotel/call-status` → 405 on GET (POST-only), and
`/webhooks/exotel/audio-stream` → 404 on GET because it is WebSocket-only.
Re-check by hand if a channel route is ever renamed.

## Current release boundary

The integration branch is at migration head `0018` and includes authenticated
Exotel callbacks, provider-CallSid correlation, database-bound tenant admission,
and a typed media-stream handoff. Those facts do not by themselves prove a
patient can complete a voice call.

Before advertising the number, the exact deployed SHA must still prove:

- the application mounted the canonical voice runtime rather than rejecting or
  draining the admitted stream;
- the selected LLM/STT/TTS configuration initialized successfully;
- DPDP notice playback completed, evidence persisted to the `0018` columns,
  greeting followed, and only then STT opened;
- Exotel's real authentication carrier, start-frame shape, codec, sample rate,
  media output, and interruption behavior match the configured adapter;
- one human Tamil call committed the correct appointment and produced durable
  notification evidence.

The edge may be stood up for controlled integration, but it is not a patient-
ready voice service until those hosted gates execute.
