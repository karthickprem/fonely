#!/usr/bin/env python3
"""Validate the public edge before a provider is pointed at it.

Answers one question: if we expose this configuration to the internet, does
the surface providers can reach match the surface we intended, and is
everything else closed?

Checks configuration coherence only. It does not dial a phone and it does not
prove a call works. Run it before registering webhook URLs, and again after
any change to the Caddyfile or the router mounting conditions.

    python3 scripts/check-public-edge.py --env-file .env.staging

Exits non-zero on any failure. Never prints a secret value — presence only.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CADDYFILE = REPO_ROOT / "deploy" / "Caddyfile"

# Paths the edge is allowed to forward. Anything the application mounts that is
# not listed here must be unreachable from the internet.
INTENDED_PUBLIC = {
    "/webhooks/whatsapp",
    "/webhooks/exotel/call-status",
    "/webhooks/exotel/audio-stream",
    "/health/live",
}

# Paths that must never be publicly reachable, with the reason, so a failure
# explains itself to whoever is on call rather than just naming a path.
MUST_STAY_PRIVATE = {
    "/metrics": "operational counters, unauthenticated on main",
    "/health/alerts": "internal failure state, unauthenticated on main",
    "/health/ready": "dependency topology",
    "/internal": "authoritative mutation API",
}

# Variables each router mounts on, from create_app(). A router whose variable is
# absent is silently not mounted — the reason this check exists.
ROUTER_GATES = {
    "WHATSAPP_VERIFY_TOKEN": "WhatsApp owner channel",
    "EXOTEL_WEBHOOK_SECRET": "Exotel telephony",
    "INTERNAL_API_SECRET": "internal API",
}

EDGE_REQUIRED = {
    "FONELY_PUBLIC_DOMAIN": "DNS name providers will call",
    "FONELY_ACME_EMAIL": "certificate expiry notices",
}


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.warnings: list[str] = []
        self.not_checked_items: list[str] = []

    def ok(self, message: str) -> None:
        print(f"  ok       {message}")

    def warn(self, message: str) -> None:
        self.warnings.append(message)
        print(f"  warn     {message}")

    def fail(self, message: str) -> None:
        self.failures.append(message)
        print(f"  FAIL     {message}")

    def not_checked(self, message: str) -> None:
        """A check this script structurally cannot perform.

        Distinct from ok and from warn on purpose. An operator reading a clean
        run must be able to tell "verified" from "not verifiable here", or the
        absence of a check reads as a passing check.
        """
        self.not_checked_items.append(message)
        print(f"  NOT RUN  {message}")


def parse_env_file(path: Path) -> dict[str, str]:
    """Read KEY=VALUE lines. Values are used for structure checks only."""
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def caddy_handled_paths(caddyfile: str) -> set[str]:
    """Extract the paths the edge forwards.

    Only `handle` blocks containing a reverse_proxy forward traffic; a bare
    `handle { respond 404 }` is the catch-all and must not count as exposure.
    """
    handled: set[str] = set()
    for match in re.finditer(r"handle\s+(\S+)\s*\{(.*?)\n\t\}", caddyfile, re.DOTALL):
        path, body = match.group(1), match.group(2)
        if "reverse_proxy" in body:
            handled.add(path.rstrip("*"))
    return handled


def check_edge_config(env: dict[str, str], report: Report) -> None:
    print("\nedge configuration")
    for key, purpose in EDGE_REQUIRED.items():
        if env.get(key):
            report.ok(f"{key} set ({purpose})")
        else:
            report.fail(f"{key} missing — {purpose}")

    domain = env.get("FONELY_PUBLIC_DOMAIN", "")
    if domain in {"localhost", "127.0.0.1"} or domain.endswith(".local"):
        report.fail(
            f"FONELY_PUBLIC_DOMAIN={domain} is not internet-resolvable; "
            "certificate issuance needs an inbound HTTP-01 challenge"
        )
    elif domain and "." not in domain:
        report.fail(f"FONELY_PUBLIC_DOMAIN={domain} is not a fully qualified name")


def check_router_gates(env: dict[str, str], report: Report) -> None:
    print("\nrouter mounting")
    for key, description in ROUTER_GATES.items():
        if env.get(key):
            report.ok(f"{description} will mount ({key} set)")
        else:
            report.warn(f"{description} will NOT mount — {key} unset")


def check_number_mappings(env: dict[str, str], report: Report) -> None:
    """Report on tenant binding, which is no longer an environment concern.

    Until migration 0017 this function parsed EXOTEL_NUMBER_MAPPINGS and could
    tell you, from the env file alone, which number reached which clinic. That
    binding now lives in business_channel_identities, so this script cannot
    see it without database credentials it deliberately does not take.

    What it can still do is catch the two ways an operator gets this wrong:
    leaving the dead variable in place and believing it still binds anything,
    and registering no numbers at all while telephony is mounted.
    """
    print("\ntenant binding")

    if env.get("EXOTEL_NUMBER_MAPPINGS"):
        report.fail(
            "EXOTEL_NUMBER_MAPPINGS is set but no longer read by anything. "
            "Since migration 0017 the dialled-number binding is a row in "
            "business_channel_identities. Leaving this variable in place means "
            "believing calls are bound when they are not — remove it and "
            "register the number through POST /internal/v1/businesses/channel-identity"
        )
        return

    if not env.get("EXOTEL_WEBHOOK_SECRET"):
        report.warn("telephony not configured (EXOTEL_WEBHOOK_SECRET unset)")
        return

    report.not_checked(
        "which clinic each dialled number reaches — it is a database row, not "
        "config. Verify against the running system before the first real call:\n"
        "             psql \"$DATABASE_URL\" -c \"SELECT business_id, "
        "external_identifier, status, is_primary FROM business_channel_identities "
        "WHERE provider = 'exotel'\"\n"
        "           An unregistered number is refused with 404 at the ringing "
        "webhook and its audio stream is then refused as an unobserved call, so "
        "the failure is a clinic whose calls do not connect — never a call "
        "landing in the wrong clinic's diary."
    )


def check_exposure(report: Report) -> None:
    print("\npublic exposure")
    if not CADDYFILE.exists():
        report.fail(f"{CADDYFILE.relative_to(REPO_ROOT)} not found")
        return

    handled = caddy_handled_paths(CADDYFILE.read_text(encoding="utf-8"))

    for path in sorted(INTENDED_PUBLIC):
        if path in handled:
            report.ok(f"{path} forwarded")
        else:
            report.fail(f"{path} is NOT forwarded — the provider cannot reach it")

    for path, reason in sorted(MUST_STAY_PRIVATE.items()):
        exposed = [h for h in handled if h == path or h.startswith(path.rstrip("/") + "/")]
        if exposed:
            report.fail(f"{path} is publicly forwarded — {reason}")
        else:
            report.ok(f"{path} not exposed ({reason})")

    for path in sorted(handled - INTENDED_PUBLIC):
        report.fail(f"{path} is forwarded but not in the intended public surface")


def check_stream_transport(report: Report) -> None:
    print("\naudio stream transport")
    text = CADDYFILE.read_text(encoding="utf-8") if CADDYFILE.exists() else ""
    match = re.search(r"handle /webhooks/exotel/audio-stream\s*\{(.*?)\n\t\}", text, re.DOTALL)
    if not match:
        report.fail("no handle block for /webhooks/exotel/audio-stream")
        return
    body = match.group(1)
    if "read_timeout 0" in body and "write_timeout 0" in body:
        report.ok("no read/write timeout — a long call will not be cut off mid-conversation")
    else:
        report.fail("audio stream has a finite timeout; a long call would be dropped by the edge")
    if "flush_interval -1" in body:
        report.ok("response buffering disabled — audio frames forward immediately")
    else:
        report.fail("flush_interval not disabled; buffered audio arrives in bursts and the agent stutters")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()

    if not args.env_file.exists():
        print(f"error: {args.env_file} not found", file=sys.stderr)
        return 2

    env = parse_env_file(args.env_file)
    report = Report()

    check_edge_config(env, report)
    check_router_gates(env, report)
    check_number_mappings(env, report)
    check_exposure(report)
    check_stream_transport(report)

    print()
    if report.failures:
        print(f"{len(report.failures)} failure(s) — do not point a provider at this deployment")
        return 1
    parts = []
    if report.warnings:
        parts.append(f"{len(report.warnings)} warning(s)")
    if report.not_checked_items:
        # Named in the summary line, not just inline, so a clean run cannot be
        # skim-read as "everything verified".
        parts.append(f"{len(report.not_checked_items)} check(s) NOT RUN")
    print("passed" + (f" with {' and '.join(parts)}" if parts else ""))
    print("configuration only — a real inbound call is the only proof a call works")
    return 0


if __name__ == "__main__":
    sys.exit(main())
