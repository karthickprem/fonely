#!/usr/bin/env python3
"""Prove that the configured demo clinic actually takes a booking.

Run `scripts/seed-demo-clinic.py` first; this checks the four things that
decide whether a patient calling the clinic's number gets a real appointment
or a plausible-sounding lie:

  1. A slot inside the clinic's afternoon closure is refused. Split shifts are
     the normal Indian clinic pattern and the single thing a naive booking
     agent gets wrong -- it offers 3pm, the clinic is shut, the patient
     arrives to a locked door.
  2. A slot inside real opening hours is proposed with the exact facts the
     agent must read back, and confirming it commits an appointment.
  3. Confirming again with the same idempotency key returns the same
     appointment rather than booking a second one. Phone calls drop; the
     retry must not double-book.
  4. A second patient asking for the same doctor at the same time is refused.

Exit code is 0 only if all four hold.

    export FONELY_INTERNAL_API_SECRET=...
    python3 scripts/prove-booking.py \
        --base-url http://127.0.0.1:8000 \
        --database-url postgresql+asyncpg://user:pw@host/db \
        --business-id 1
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

IST = ZoneInfo("Asia/Kolkata")

PATIENT_PHONE = "+919840000042"
PATIENT_NAME = "Meena Ramesh"
OTHER_PATIENT_PHONE = "+919840000043"


class ProofFailure(RuntimeError):
    """Raised when the booking path did not behave as a clinic requires."""


def next_weekday(base: datetime, weekday: int) -> datetime:
    """The next occurrence of `weekday` strictly after `base`."""
    ahead = (weekday - base.weekday()) % 7 or 7
    return base + timedelta(days=ahead)


async def load_ids(database_url: str, business_id: int) -> tuple[int, int, str, str]:
    """Pick the consultation service and a dentist eligible to perform it."""
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT s.id, r.id, s.name, r.name "
                        "FROM services s "
                        "JOIN service_resource_eligibility e "
                        "  ON e.service_id = s.id AND e.business_id = s.business_id "
                        "JOIN resources r "
                        "  ON r.id = e.resource_id AND r.business_id = s.business_id "
                        "WHERE s.business_id = :bid AND s.is_active AND r.is_active "
                        "  AND e.is_active AND s.name = 'Consultation' "
                        "ORDER BY r.id LIMIT 1"
                    ),
                    {"bid": business_id},
                )
            ).first()
            if row is None:
                raise ProofFailure(
                    f"business {business_id} has no active Consultation with an eligible "
                    "resource -- run scripts/seed-demo-clinic.py first"
                )
            return int(row[0]), int(row[1]), str(row[2]), str(row[3])
    finally:
        await engine.dispose()


async def read_appointments(database_url: str, business_id: int) -> list[tuple]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as conn:
            return list(
                (
                    await conn.execute(
                        text(
                            "SELECT id, service_id, resource_id, start_at, status, "
                            "customer_phone FROM appointments "
                            "WHERE business_id = :bid ORDER BY id"
                        ),
                        {"bid": business_id},
                    )
                ).all()
            )
    finally:
        await engine.dispose()


class BookingClient:
    def __init__(self, base_url: str, secret: str, business_id: int, phone: str) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {secret}",
                "X-Business-ID": str(business_id),
                "X-Actor-Phone": phone,
                "X-Actor-Role": "customer",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def propose(
        self,
        service_id: int,
        resource_id: int,
        start_at: datetime,
        idempotency_key: str,
        customer_phone: str,
        expires_at: datetime,
    ) -> httpx.Response:
        return await self._client.post(
            "/internal/v1/appointment-proposals",
            json={
                "service_id": service_id,
                "resource_id": resource_id,
                "start_at": start_at.isoformat(),
                "customer_name": PATIENT_NAME,
                "customer_phone": customer_phone,
                "reason": "Tooth pain, wants a checkup",
                "idempotency_key": idempotency_key,
                "expires_at": expires_at.isoformat(),
            },
        )

    async def confirm(self, pending_action_id: int, version: int) -> httpx.Response:
        return await self._client.post(
            f"/internal/v1/appointment-proposals/{pending_action_id}/confirm",
            json={"expected_version": version},
        )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--business-id", type=int, required=True)
    parser.add_argument("--now", help="ISO datetime to treat as now (default: actual now)")
    parser.add_argument(
        "--run-tag",
        help="Suffix for idempotency keys so repeat runs are distinct requests",
    )
    args = parser.parse_args()

    secret = os.environ.get("FONELY_INTERNAL_API_SECRET")
    if not secret:
        print("FONELY_INTERNAL_API_SECRET is not set", file=sys.stderr)
        return 2

    now = datetime.fromisoformat(args.now) if args.now else datetime.now(IST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)

    service_id, resource_id, service_name, resource_name = await load_ids(
        args.database_url, args.business_id
    )
    print(f"clinic: {service_name} with {resource_name}")

    # Each run is a different patient calling, so its keys must be distinct;
    # reusing them would collide with the previous run rather than test anything.
    run_tag = args.run_tag or now.strftime("%Y%m%d%H%M%S")
    monday = next_weekday(now, 0)
    closed_slot = monday.replace(hour=15, minute=0, second=0, microsecond=0)
    open_slot = monday.replace(hour=10, minute=0, second=0, microsecond=0)
    # A hold lasts as long as a phone call, not a day.
    hold_until = now + timedelta(minutes=10)
    print(f"target Monday: {monday.date().isoformat()}\n")

    before = await read_appointments(args.database_url, args.business_id)
    client = BookingClient(args.base_url, secret, args.business_id, PATIENT_PHONE)
    failures: list[str] = []

    try:
        # 1. The afternoon closure.
        response = await client.propose(
            service_id, resource_id, closed_slot, f"proof-closed-gap-{run_tag}", PATIENT_PHONE, hold_until
        )
        if response.status_code == 201:
            failures.append(
                f"15:00 was accepted ({closed_slot:%a %d %b %H:%M}) but the clinic is "
                "closed 13:00-17:00 -- the patient would arrive to a locked door"
            )
            print(f"1. closed-hours slot   FAIL  accepted with {response.status_code}")
        else:
            detail = response.json().get("detail", response.text[:120])
            print(f"1. closed-hours slot   ok    refused {response.status_code} ({detail})")

        # 2. A real slot, proposed and committed.
        response = await client.propose(
            service_id, resource_id, open_slot, f"proof-open-slot-{run_tag}", PATIENT_PHONE, hold_until
        )
        if response.status_code != 201:
            detail = response.json().get("detail", response.text[:200])
            failures.append(
                f"10:00 Monday was refused ({response.status_code}: {detail}) but the "
                "clinic is open 09:30-13:00 -- no patient can book at all"
            )
            print(f"2. open-hours slot     FAIL  refused {response.status_code} ({detail})")
            return _report(failures)

        proposal = response.json()
        facts = proposal["confirmation_facts"]
        print(
            f"2. open-hours slot     ok    proposed #{proposal['pending_action_id']} "
            f"held={proposal['slot_is_held']}"
        )
        print(f"   facts read back to the patient: {facts}")

        response = await client.confirm(proposal["pending_action_id"], proposal["version"])
        if response.status_code != 200:
            detail = response.json().get("detail", response.text[:200])
            failures.append(f"confirmation failed ({response.status_code}: {detail})")
            print(f"3. confirm             FAIL  {response.status_code} ({detail})")
            return _report(failures)

        committed = response.json()
        if "error_code" in committed:
            failures.append(f"confirmation returned retryable failure {committed['error_code']}")
            print(f"3. confirm             FAIL  retryable {committed['error_code']}")
            return _report(failures)

        appointment_id = committed.get("appointment_id")
        print(f"3. confirm             ok    appointment #{appointment_id} committed")

        # 4. The dropped call: same key, again.
        response = await client.propose(
            service_id, resource_id, open_slot, f"proof-open-slot-{run_tag}", PATIENT_PHONE, hold_until
        )
        after_retry = await read_appointments(args.database_url, args.business_id)
        new_rows = len(after_retry) - len(before)
        if new_rows != 1:
            failures.append(
                f"retrying with the same idempotency key produced {new_rows} appointments, "
                "expected exactly 1 -- a dropped call would double-book the patient"
            )
            print(f"4. idempotent retry    FAIL  {new_rows} appointments exist")
        else:
            print(f"4. idempotent retry    ok    still exactly 1 appointment ({response.status_code})")

        # 5. A different patient wanting the same doctor at the same time.
        other = BookingClient(args.base_url, secret, args.business_id, OTHER_PATIENT_PHONE)
        try:
            response = await other.propose(
                service_id, resource_id, open_slot, f"proof-double-book-{run_tag}", OTHER_PATIENT_PHONE, hold_until
            )
            if response.status_code == 201:
                confirmed = await other.confirm(
                    response.json()["pending_action_id"], response.json()["version"]
                )
                if confirmed.status_code == 200 and "error_code" not in confirmed.json():
                    failures.append(
                        "a second patient booked the same doctor at the same time -- "
                        "two people will arrive for one chair"
                    )
                    print("5. double booking      FAIL  second appointment committed")
                else:
                    print(f"5. double booking      ok    refused at confirm ({confirmed.status_code})")
            else:
                detail = response.json().get("detail", response.text[:120])
                print(f"5. double booking      ok    refused {response.status_code} ({detail})")
        finally:
            await other.aclose()

        print("\nappointments now in the database:")
        for row in await read_appointments(args.database_url, args.business_id):
            print(f"  #{row[0]} service={row[1]} resource={row[2]} {row[3]} {row[4]} {row[5]}")

        return _report(failures)
    finally:
        await client.aclose()


def _report(failures: list[str]) -> int:
    if failures:
        print(f"\n{len(failures)} failure(s):")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nOK: the clinic refuses closed hours, commits a real booking, survives a "
          "retry, and will not double-book.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
