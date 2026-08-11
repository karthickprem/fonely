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
EXOTEL_NUMBER_MAPPINGS={"+918000000000": 1}
```

`EXOTEL_NUMBER_MAPPINGS` maps each Exotel virtual number to the `business_id`
that owns it. This is how an inbound call is bound to a tenant — from the
*dialled* number, which the carrier controls, never from anything in the
request body, which a caller can forge. An unmapped number is refused with 404
rather than guessed at.

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

```bash
python3 scripts/check-public-edge.py --env-file .env.staging
```

It checks configuration coherence — that the variables the routers gate on are
present, that the number mapping parses and maps to plausible tenant ids, that
the Caddy allowlist forwards every intended provider path and no private one,
and that the audio stream has no timeout or buffering that would cut a call
short. It reads the Caddyfile and the env file; it does not import the
application, so it cannot notice a route that is renamed in code. It does not
dial a phone. Only a real call proves a real call works.

The forwarded paths were checked against a booted application on `cc3aa65`:
`/health/live` → 200, `/webhooks/whatsapp` → 403 without a valid token,
`/webhooks/exotel/call-status` → 405 on GET (POST-only), and
`/webhooks/exotel/audio-stream` → 404 on GET because it is WebSocket-only.
Re-check by hand if a channel route is ever renamed.

## Known gaps

These are true of `main` as of migration head 0015 and are being closed:

- `/webhooks/exotel/call-status` performs **no signature or secret
  verification**. Anyone who learns the URL can post call events. Do not
  publish the number to patients until this is fixed.
- `/webhooks/exotel/audio-stream` accepts the socket and discards every frame.
  There is no speech pipeline behind it yet.
- Call-status correlation matches on the most recently opened call for a phone
  number rather than on the provider call id, which is wrong when the same
  patient calls twice.

The edge is safe to stand up now — it is how the developers get a reachable URL
to build against. It is not safe to advertise the number until the three items
above are closed.
