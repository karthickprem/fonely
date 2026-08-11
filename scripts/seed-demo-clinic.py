#!/usr/bin/env python3
"""Configure the demo dental clinic through the real onboarding API.

This drives the mounted internal API over HTTP -- draft, submit for review,
approve, activate -- exactly as an operator or the WhatsApp onboarding seam
would. It deliberately does not insert services, resources or schedules
itself: the whole point is to prove the supported path produces a bookable
clinic, so anything this script wrote by hand would prove nothing.

Every step is now a supported one. Creating the clinic itself used to be the
exception -- no mounted route could do it, so the script inserted the
business and owner rows by hand -- but POST /internal/v1/businesses exists
now and --provision uses it.

Usage:

    export INTERNAL_API_SECRET=...               # never passed on argv;
                                                 # the same variable the server reads
    python3 scripts/seed-demo-clinic.py \
        --base-url http://127.0.0.1:8000 \
        --database-url postgresql+asyncpg://user:pw@host/db \
        --provision

Add --verify-reactivation to activate a second, edited draft and report
whether the configuration was replaced or duplicated.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[1]
CLINIC_FILE = REPO_ROOT / "deploy" / "demo-clinic" / "clinic.json"

WEEKDAY_INDEX = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


class SeedError(RuntimeError):
    """Raised when the clinic could not be configured through the real path."""


# ---------------------------------------------------------------------------
# Draft construction
# ---------------------------------------------------------------------------


def _provenance(source_id: str, *fields: str) -> dict[str, Any]:
    """Record where each configuration fact came from.

    Activation refuses any required field whose origin is unrecorded, and
    refuses a recorded origin that carries no evidence. That is the right
    guarantee: a clinic's hours and prices are answerable facts, and when a
    patient is quoted the wrong price somebody has to be able to say who said
    so. Here the source is the operator entering what the owner stated during
    onboarding, which is what owner_provided marks.
    """
    entry = {
        "review_status": "owner_confirmed",
        "sources": [
            {
                "source_type": "operator_entry",
                "source_id": source_id,
                "owner_provided": True,
            }
        ],
    }
    return {"fields": [[name, entry] for name in fields]}


def _periods(schedule: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten the clinic's weekly hours into canonical schedule periods.

    A split shift is two periods on one day. That is the normal Indian clinic
    pattern and the single thing a naive booking agent gets wrong, so it is
    carried through rather than flattened into one long opening.
    """
    periods: list[dict[str, Any]] = []
    for day_name, shifts in schedule.items():
        if day_name.startswith("_"):
            continue
        index = WEEKDAY_INDEX[day_name]
        for shift in shifts:
            periods.append(
                {
                    "day": index,
                    "start": shift["open"],
                    "end": shift["close"],
                    "is_closed": False,
                }
            )
    return periods


def build_draft(clinic: dict[str, Any], draft_key: str) -> dict[str, Any]:
    """Translate the clinic file into a canonical onboarding draft."""
    business = clinic["business"]
    schedule_periods = _periods(clinic["operating_schedule"])

    services = []
    for svc in clinic["services"]:
        eligible = [res["key"] for res in clinic["resources"] if svc["key"] in res["services"]]
        if not eligible:
            raise SeedError(f"service {svc['key']!r} has no eligible resource")
        services.append(
            {
                "key": svc["key"],
                "name": svc["name"],
                "location_keys": ["main"],
                "duration_minutes": svc["duration_minutes"],
                "is_active": True,
                "price": {
                    "kind": "fixed",
                    "currency": clinic["business"]["currency"],
                    "amount": str(svc["price_inr"]),
                    "note": svc.get("price_note"),
                },
                "eligible_resource_keys": eligible,
                "requires_resource": True,
                "provenance": _provenance(
                    f"owner-stated:service:{svc['key']}",
                    "name",
                    "duration_minutes",
                    "is_active",
                    "price",
                    "eligible_resource_keys",
                    "requires_resource",
                    "location_keys",
                ),
            }
        )

    resources = [
        {
            "key": res["key"],
            "display_name": res["name"],
            "resource_type": "staff",
            "location_keys": ["main"],
            "service_keys": res["services"],
            "is_active": True,
            # Practitioners keep clinic hours unless the owner says otherwise.
            # Resource-level hours are left empty rather than invented, so
            # availability derives from the one schedule the owner confirmed.
            "schedule": {"periods": [], "exceptions": []},
            "provenance": _provenance(
                f"owner-stated:resource:{res['key']}",
                "display_name",
                "resource_type",
                "is_active",
                "location_keys",
                "service_keys",
                "schedule",
            ),
        }
        for res in clinic["resources"]
    ]

    policies = clinic["policies"]
    return {
        "draft_id": draft_key,
        "business_name": business["name"],
        "business_category": "clinic",
        "default_timezone": business["timezone"],
        "default_currency": business["currency"],
        "preferred_languages": list(business["languages"]),
        "policy": {
            "advance_booking_days": policies["advance_booking_days"],
            "cancellation_cutoff_minutes": policies["cancellation_notice_hours"] * 60,
            "walk_in_allowed": policies["walk_ins_accepted"],
            "resource_selection_required": False,
            "owner_review_required": True,
            "provenance": _provenance(
                "owner-stated:policy",
                "advance_booking_days",
                "cancellation_cutoff_minutes",
                "walk_in_allowed",
                "resource_selection_required",
            ),
        },
        "locations": [
            {
                "key": "main",
                "display_name": business["name"],
                "address": {"city": "Chennai", "state": "Tamil Nadu", "country": "IN"},
                "is_active": True,
                "schedule": {"periods": schedule_periods, "exceptions": []},
                "provenance": _provenance(
                    "owner-stated:location:main",
                    "display_name",
                    "is_active",
                    "schedule",
                    "address.city",
                    "address.state",
                ),
            }
        ],
        "services": services,
        "resources": resources,
        "provenance": _provenance(
            "owner-stated:business",
            "business_name",
            "business_category",
            "default_timezone",
            "default_currency",
        ),
    }


# ---------------------------------------------------------------------------
# Provisioning (was the gap)
# ---------------------------------------------------------------------------


async def provision_business(base_url: str, secret: str, clinic: dict[str, Any]) -> tuple[int, int]:
    """Create the clinic through the supported provisioning route.

    This used to insert into `businesses` and `business_users` directly,
    because no mounted route could create a tenant. `POST
    /internal/v1/businesses` now can, so the hand-written insert is gone and
    this script drives the supported path from the very first step.

    Re-running is safe: the route keys on the owner's phone and returns the
    existing clinic rather than standing up a second one.
    """
    async with httpx.AsyncClient(
        base_url=base_url.rstrip("/"),
        timeout=30.0,
        headers={"Authorization": f"Bearer {secret}"},
    ) as client:
        response = await client.post(
            "/internal/v1/businesses",
            json={
                "name": clinic["business"]["name"],
                "category": "clinic",
                "owner_phone": clinic["owner"]["phone"],
                "owner_name": clinic["owner"].get("name"),
                "timezone": clinic["business"]["timezone"],
            },
        )
    if response.status_code >= 400:
        raise SeedError(
            f"POST /internal/v1/businesses -> {response.status_code} {response.text[:400]}"
        )
    body = response.json()
    verb = "created" if body["created"] else "already existed"
    print(
        f"provisioned via API: business={body['business_id']} "
        f"owner={body['owner_user_id']} ({verb})"
    )
    return int(body["business_id"]), int(body["owner_user_id"])


async def register_whatsapp_channel(
    base_url: str, secret: str, business_id: int, phone_number_id: str
) -> None:
    """Attach the demo clinic's WhatsApp number through the supported route.

    Without this the clinic is configured but unreachable: appointment commit
    resolves the sending number from business_whatsapp_channels and refuses
    with whatsapp_mapping_missing when there is no row. Re-running is safe --
    re-registering the same number for the same tenant is idempotent.
    """
    async with httpx.AsyncClient(
        base_url=base_url.rstrip("/"),
        timeout=30.0,
        headers={
            "Authorization": f"Bearer {secret}",
            "X-Business-ID": str(business_id),
        },
    ) as client:
        response = await client.post(
            "/internal/v1/businesses/whatsapp-channel",
            json={"phone_number_id": phone_number_id, "make_primary": True},
        )
    if response.status_code >= 400:
        raise SeedError(
            "POST /internal/v1/businesses/whatsapp-channel -> "
            f"{response.status_code} {response.text[:400]}"
        )
    print(f"whatsapp channel registered for business={business_id}")


# ---------------------------------------------------------------------------
# The real path
# ---------------------------------------------------------------------------


class OnboardingClient:
    """Thin wrapper over the mounted internal onboarding routes."""

    def __init__(self, base_url: str, secret: str, business_id: int, actor_user_id: int) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {secret}",
                "X-Business-ID": str(business_id),
                "X-Actor-User-ID": str(actor_user_id),
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post(f"/internal/v1{path}", json=payload)
        if response.status_code >= 400:
            raise SeedError(f"POST {path} -> {response.status_code} {response.text[:400]}")
        return response.json()

    async def create_draft(self, draft_data: dict[str, Any]) -> dict[str, Any]:
        return await self._post("/onboarding/drafts", {"draft_data": draft_data})

    async def submit_review(self, draft_id: int, version: int) -> dict[str, Any]:
        return await self._post(
            f"/onboarding/drafts/{draft_id}/submit-review", {"expected_version": version}
        )

    async def approve(self, draft_id: int, version: int) -> dict[str, Any]:
        return await self._post(
            f"/onboarding/drafts/{draft_id}/approve", {"expected_version": version}
        )

    async def activate(self, draft_id: int, version: int) -> dict[str, Any]:
        return await self._post(
            f"/onboarding/drafts/{draft_id}/activate", {"expected_version": version}
        )


async def run_activation(client: OnboardingClient, draft_data: dict[str, Any]) -> dict[str, Any]:
    """Take one draft all the way from intake to activated configuration."""
    draft = await client.create_draft(draft_data)
    draft_id = draft["id"]
    print(f"  draft {draft_id} created        status={draft['status']} v{draft['version']}")

    reviewed = await client.submit_review(draft_id, draft["version"])
    print(f"  submitted for review          status={reviewed['status']} v{reviewed['version']}")

    approved = await client.approve(draft_id, reviewed["version"])
    print(f"  approved                      status={approved['status']} v{approved['version']}")

    result = await client.activate(draft_id, approved["version"])
    if not result.get("success"):
        raise SeedError(f"activation failed: {result.get('error')}")
    print(
        "  activated                     "
        f"services={result['services_count']} resources={result['resources_count']} "
        f"eligibilities={result['eligibilities_count']} schedules={result['schedules_count']}"
    )
    return result


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


async def read_configuration(database_url: str, business_id: int) -> dict[str, Any]:
    """Read back what the tenant actually has, scoped by business_id."""
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as conn:

            async def count(table: str) -> int:
                sql = f"SELECT count(*) FROM {table} WHERE business_id = :bid AND is_active"
                return int((await conn.execute(text(sql), {"bid": business_id})).scalar_one())

            hours = (
                await conn.execute(
                    text(
                        "SELECT day_of_week, open_time, close_time FROM operating_schedules "
                        "WHERE business_id = :bid AND resource_id IS NULL AND is_active "
                        "ORDER BY day_of_week, open_time"
                    ),
                    {"bid": business_id},
                )
            ).all()

            return {
                "services": await count("services"),
                "resources": await count("resources"),
                "eligibilities": await count("service_resource_eligibility"),
                "hours": [(int(d), str(o), str(c)) for d, o, c in hours],
            }
    finally:
        await engine.dispose()


def describe_hours(hours: list[tuple[int, str, str]]) -> str:
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    by_day: dict[int, list[str]] = {}
    for day, open_time, close_time in hours:
        by_day.setdefault(day, []).append(f"{open_time[:5]}-{close_time[:5]}")
    return "; ".join(f"{names[d]} {', '.join(v)}" for d, v in sorted(by_day.items()))


# ---------------------------------------------------------------------------


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Running Fonely backend")
    parser.add_argument("--database-url", required=True, help="SQLAlchemy async URL")
    parser.add_argument("--business-id", type=int, help="Existing tenant to configure")
    parser.add_argument("--actor-user-id", type=int, help="Owner user performing the change")
    parser.add_argument(
        "--provision",
        "--bootstrap",
        dest="provision",
        action="store_true",
        help="Create the clinic through POST /internal/v1/businesses if it does not exist",
    )
    parser.add_argument(
        "--whatsapp-phone-number-id",
        help=(
            "Provider phone_number_id to attach to this clinic. Without a "
            "registered channel the clinic is configured but unreachable and "
            "booking commit refuses with whatsapp_mapping_missing."
        ),
    )
    parser.add_argument(
        "--verify-reactivation",
        action="store_true",
        help="Activate a second edited draft and report whether hours were replaced",
    )
    args = parser.parse_args()

    # Settings carries no env_prefix, so the server reads INTERNAL_API_SECRET.
    # The prefixed spelling stays as a fallback for anyone already exporting it.
    secret = os.environ.get("INTERNAL_API_SECRET") or os.environ.get("FONELY_INTERNAL_API_SECRET")
    if not secret:
        print(
            "INTERNAL_API_SECRET is not set (the same variable the server reads)",
            file=sys.stderr,
        )
        return 2

    clinic = json.loads(CLINIC_FILE.read_text())

    if args.provision:
        business_id, actor_user_id = await provision_business(args.base_url, secret, clinic)
    elif args.business_id and args.actor_user_id:
        business_id, actor_user_id = args.business_id, args.actor_user_id
    else:
        print("pass --provision, or both --business-id and --actor-user-id", file=sys.stderr)
        return 2

    if args.whatsapp_phone_number_id:
        await register_whatsapp_channel(
            args.base_url, secret, business_id, args.whatsapp_phone_number_id
        )
    else:
        print(
            "no --whatsapp-phone-number-id given: clinic will be configured but "
            "unreachable, and booking commit will refuse with whatsapp_mapping_missing"
        )

    client = OnboardingClient(args.base_url, secret, business_id, actor_user_id)
    try:
        print("\nconfiguring through the real onboarding API")
        await run_activation(client, build_draft(clinic, "demo-clinic-v1"))

        after_first = await read_configuration(args.database_url, business_id)
        print(
            f"\nin the database: services={after_first['services']} "
            f"resources={after_first['resources']} "
            f"eligibilities={after_first['eligibilities']} "
            f"hours_rows={len(after_first['hours'])}"
        )
        print(f"  hours: {describe_hours(after_first['hours'])}")

        if not args.verify_reactivation:
            return 0

        # The owner changes the clinic's hours -- the ordinary case, and the
        # one the product is built around. Everything else stays identical.
        print("\nowner edits hours and activates again (Saturday extended to 15:00)")
        edited = json.loads(json.dumps(clinic))
        edited["operating_schedule"]["saturday"] = [{"open": "09:30", "close": "15:00"}]
        await run_activation(client, build_draft(edited, "demo-clinic-v2"))

        after_second = await read_configuration(args.database_url, business_id)
        print(
            f"\nin the database: services={after_second['services']} "
            f"resources={after_second['resources']} "
            f"eligibilities={after_second['eligibilities']} "
            f"hours_rows={len(after_second['hours'])}"
        )
        print(f"  hours: {describe_hours(after_second['hours'])}")

        first_rows = len(after_first["hours"])
        second_rows = len(after_second["hours"])
        if second_rows > first_rows:
            print(
                f"\nFAIL: hours were added, not replaced ({first_rows} -> {second_rows} rows).\n"
                "The clinic's superseded hours are still active, so the agent will\n"
                "offer slots the owner has already withdrawn."
            )
            return 1

        print("\nOK: re-activation replaced the configuration rather than duplicating it.")
        return 0
    finally:
        await client.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
