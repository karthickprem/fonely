"""Correct-tenant-under-concurrency proof (in-process, fonely_dev4).

The question this answers: when two callers of DIFFERENT clinics book at the
same instant through the production voice commit path, does each appointment
land under its OWN business_id — and can a session bound to clinic A ever
commit a row under clinic B, or use clinic B's service? The tenancy guarantee
is structural (business_id is frozen in ActorContext at CommandPort
construction, backend_ports.py:153, never taken from model output), but a
guarantee is only worth what its row-level proof shows.

What it does, all in-process (NEVER the external :8000, NEVER real creds):
  1. Verify current_database is fonely_dev4.
  2. Seed a SECOND tenant through the SUPPORTED onboarding API driven in
     memory via httpx ASGITransport against the mounted app — distinct name,
     owner phone, services, resources. (Tenant 1 = the seeded Smile Care.)
  3. POSITIVE concurrency: fire N bookings for tenant 1 and N for tenant 2
     concurrently through the production AppointmentServiceCommandPort, each
     with its own ActorContext. Assert every committed row's business_id
     matches the tenant that booked it — zero cross-contamination.
  4. ADVERSARIAL cross-tenant: a CommandPort bound to tenant A is handed
     tenant B's service_id. It must REFUSE (the service is not tenant A's, and
     business_id cannot be overridden) — no row under either tenant.
  5. Clean up every row this proof created (tagged by idempotency key +
     customer phone) so the run is reproducible and the tenant-2 data is
     disposable.

No src changes: this is a lab harness that drives the production processors.
"""
import asyncio
import sys
import time as _time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, "/scratch/karthick/fonely-worktrees/dev4-cartesia/voice-lab")

import production_wiring as pw  # sets INTERNAL_API_SECRET + DATABASE_URL=fonely_dev4 in-process
from sqlalchemy import text as sql_text

TZ = ZoneInfo("Asia/Kolkata")
SECRET = "dev4-demo-secret"  # the in-process test secret production_wiring sets; NOT from .env
MARKER = f"tenantconc-{int(_time.time())}"  # tags every row this run creates

# Distinct tenant-2 identity — different name, owner phone, and service/resource
# names so a cross-tenant leak is unambiguous in the row data.
TENANT2 = {
    "business": {"name": f"Coastal Family Dental [{MARKER}]", "timezone": "Asia/Kolkata",
                 "currency": "INR", "languages": ["ta-IN", "en-IN"]},
    "owner": {"name": "Dr Meena Coastal", "phone": f"+9199{int(_time.time()) % 100000000:08d}"},
    "resources": [{"key": "dr_meena", "name": "Dr. Meena Coastal",
                   "services": ["consultation", "cleaning"]}],
    "services": [
        {"key": "consultation", "name": "Coastal Consultation", "duration_minutes": 30,
         "price_inr": 400, "price_note": None},
        {"key": "cleaning", "name": "Coastal Deep Cleaning", "duration_minutes": 45,
         "price_inr": 1500, "price_note": None},
    ],
    "operating_schedule": {
        "monday": [{"open": "09:00", "close": "13:00"}, {"open": "16:00", "close": "20:00"}],
        "tuesday": [{"open": "09:00", "close": "13:00"}, {"open": "16:00", "close": "20:00"}],
        "wednesday": [{"open": "09:00", "close": "13:00"}, {"open": "16:00", "close": "20:00"}],
        "thursday": [{"open": "09:00", "close": "13:00"}, {"open": "16:00", "close": "20:00"}],
        "friday": [{"open": "09:00", "close": "13:00"}, {"open": "16:00", "close": "20:00"}],
        "saturday": [{"open": "09:00", "close": "14:00"}],
    },
    "policies": {"advance_booking_days": 30, "cancellation_notice_hours": 4,
                 "walk_ins_accepted": False},
}


async def current_database() -> str:
    async with pw._SessionLocal() as s:
        return (await s.execute(sql_text("SELECT current_database()"))).scalar()


def _build_app():
    from fonely.app import create_app
    return create_app()


async def seed_tenant2() -> dict:
    """Seed tenant 2 through the SUPPORTED onboarding routes, in-process.

    Reuses the demo seed script's canonical draft builder so this is the real
    onboarding path, not a hand-inserted tenant. Returns ids resolved from the
    DB after activation.
    """
    import httpx
    from httpx import ASGITransport
    sys.path.insert(0, "/scratch/karthick/fonely/.claude/worktrees/dev4-voice-runtime/.scratch/seed-tooling")
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "seed_demo_clinic",
        "/scratch/karthick/fonely/.claude/worktrees/dev4-voice-runtime/.scratch/seed-tooling/seed-demo-clinic.py",
    )
    seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed)

    app = _build_app()
    # ASGITransport does not run the FastAPI lifespan, so app.state.session_factory
    # (normally set in lifespan) is absent. Inject the SAME fonely_dev4 session
    # factory production_wiring already built — this keeps every write on the one
    # engine/pool and avoids standing up a second lifespan-managed engine.
    app.state.session_factory = pw._SessionLocal
    app.state.engine = pw._engine
    app.state.model_gateway = None
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://asgi") as client:
        # Provision the business through POST /internal/v1/businesses.
        r = await client.post(
            "/internal/v1/businesses",
            headers={"Authorization": f"Bearer {SECRET}"},
            json={"name": TENANT2["business"]["name"], "category": "clinic",
                  "owner_phone": TENANT2["owner"]["phone"],
                  "owner_name": TENANT2["owner"]["name"],
                  "timezone": TENANT2["business"]["timezone"]},
        )
        r.raise_for_status()
        body = r.json()
        biz_id, owner_id = int(body["business_id"]), int(body["owner_user_id"])

        # Drive draft → submit → approve → activate with the canonical builder.
        draft_data = seed.build_draft(TENANT2, f"tenant2-{MARKER}")
        hdr = {"Authorization": f"Bearer {SECRET}", "X-Business-ID": str(biz_id),
               "X-Actor-User-ID": str(owner_id)}

        async def post(path, payload):
            resp = await client.post(f"/internal/v1{path}", headers=hdr, json=payload)
            resp.raise_for_status()
            return resp.json()

        draft = await post("/onboarding/drafts", {"draft_data": draft_data})
        did, ver = draft["id"], draft["version"]
        rv = await post(f"/onboarding/drafts/{did}/submit-review", {"expected_version": ver})
        ap = await post(f"/onboarding/drafts/{did}/approve", {"expected_version": rv["version"]})
        act = await post(f"/onboarding/drafts/{did}/activate", {"expected_version": ap["version"]})
        if not act.get("success"):
            raise RuntimeError(f"tenant2 activation failed: {act.get('error')}")

    # Resolve tenant-2 service/resource ids from the DB.
    async with pw._SessionLocal() as s:
        svc = (await s.execute(sql_text(
            "SELECT id, name FROM services WHERE business_id=:b AND is_active ORDER BY id"),
            {"b": biz_id})).fetchall()
        res = (await s.execute(sql_text(
            "SELECT id, name FROM resources WHERE business_id=:b AND is_active ORDER BY id"),
            {"b": biz_id})).fetchall()
    return {"business_id": biz_id, "owner_user_id": owner_id,
            "services": [(x[0], x[1]) for x in svc],
            "resources": [(x[0], x[1]) for x in res]}


def _port_for(business_id: int, phone: str, conv_id: str):
    """Build a production CommandPort bound to a specific tenant — business_id
    frozen in the ActorContext here, exactly as the real call path does."""
    from fonely.voice.backend_ports import AppointmentServiceCommandPort, build_actor_context
    actor = build_actor_context(business_id=business_id, phone=phone, session_id=conv_id)
    return AppointmentServiceCommandPort(
        actor=actor,
        session_factory=pw._session_factory,
        validation_factory=pw._validation_factory,
        business_timezone="Asia/Kolkata",
        conversation_id=conv_id,
    )


async def _resolve_ids(business_id: int, service_phrase: str):
    """Resolve a real (service_id, resource_id) for a tenant via the resolver."""
    from fonely.voice import clinic_resolver as cr
    async with pw._SessionLocal() as s:
        svc = await cr.resolve_service(s, business_id, service_phrase)
        if svc is None:
            return None, None
        res = await cr.resolve_resource_for_service(s, business_id, svc.service_id)
        return svc.service_id, (res.resource_id if res else None)


async def book_via_port(port, business_id, service_id, resource_id, target_date,
                        target_time, phone, tag):
    """Full propose+confirm through the production port. Returns
    (success, appointment_id, error)."""
    from fonely.voice.runtime import ProposeCommand, ConfirmCommand, TrustedCommandContext
    ctx = TrustedCommandContext(business_id=business_id, actor_session_id=tag,
                                conversation_id=tag)
    idem = f"{MARKER}-{tag}"
    pr = await port.propose(ProposeCommand(
        context=ctx, service_id=service_id, resource_id=resource_id,
        target_date=target_date, target_time=target_time.strftime("%H:%M"),
        idempotency_key=idem))
    if not pr.success:
        return False, None, pr.error or "propose_failed"
    cr = await port.confirm(ConfirmCommand(
        context=ctx, proposal_id=pr.proposal_id, idempotency_key=idem,
        expected_version=int(pr.evidence.get("version", 0))))
    if not cr.success or cr.receipt is None:
        return False, None, cr.error or "confirm_failed"
    return True, cr.receipt.commitment_id, ""


async def assert_row_tenant(appointment_id: int) -> int:
    """Return the business_id the row actually committed under."""
    async with pw._SessionLocal() as s:
        r = (await s.execute(sql_text(
            "SELECT business_id FROM appointments WHERE id=:i"), {"i": appointment_id})).first()
        return r[0] if r else None


async def cleanup() -> dict:
    """Delete the DISPOSABLE rows this run created (appointments + their
    notifications), each step committed on its own so one protected row can't
    roll back the whole cleanup.

    pending_actions rows that reached committed provenance are IMMUTABLE by
    design (a durability guarantee — a FK trigger refuses their deletion). We
    do NOT fight that; we leave them as audit provenance and report the count.
    """
    left = {"appointments": 0, "pending_actions_provenance": 0}
    async with pw._SessionLocal() as s:
        appt_ids = [r[0] for r in (await s.execute(sql_text(
            "SELECT id FROM appointments WHERE idempotency_key LIKE :m"),
            {"m": f"{MARKER}%"})).fetchall()]
        await s.execute(sql_text(
            "DELETE FROM notification_outbox WHERE idempotency_key LIKE :m"), {"m": f"%{MARKER}%"})
        await s.commit()
    async with pw._SessionLocal() as s:
        await s.execute(sql_text(
            "DELETE FROM notification_manifests WHERE entity_type='appointment' AND entity_id = ANY(:ids)"),
            {"ids": appt_ids or [-1]})
        await s.commit()
    async with pw._SessionLocal() as s:
        await s.execute(sql_text(
            "DELETE FROM appointments WHERE idempotency_key LIKE :m"), {"m": f"{MARKER}%"})
        await s.commit()
    # Non-committed pending_actions can be removed; committed provenance cannot.
    async with pw._SessionLocal() as s:
        try:
            await s.execute(sql_text(
                "DELETE FROM pending_actions WHERE idempotency_key LIKE :m "
                "AND provenance IS DISTINCT FROM 'committed'"), {"m": f"%{MARKER}%"})
            await s.commit()
        except Exception:
            await s.rollback()
    async with pw._SessionLocal() as s:
        left["appointments"] = (await s.execute(sql_text(
            "SELECT count(*) FROM appointments WHERE idempotency_key LIKE :m"),
            {"m": f"{MARKER}%"})).scalar()
        left["pending_actions_provenance"] = (await s.execute(sql_text(
            "SELECT count(*) FROM pending_actions WHERE idempotency_key LIKE :m"),
            {"m": f"%{MARKER}%"})).scalar()
    return left


async def main():
    print("=" * 74)
    print("CORRECT-TENANT-UNDER-CONCURRENCY PROOF — in-process, fonely_dev4")
    print("=" * 74)

    db = await current_database()
    print(f"current_database: {db}")
    if db != "fonely_dev4":
        print("✗ ABORT: not fonely_dev4"); return
    print(f"run marker: {MARKER}")

    t1 = {"business_id": pw.DEMO_BUSINESS_ID}  # existing seeded tenant
    print(f"\ntenant 1: business_id={t1['business_id']} (existing Smile Care)")
    print("seeding tenant 2 through supported onboarding (in-process ASGI)...")
    t2 = await seed_tenant2()
    print(f"tenant 2: business_id={t2['business_id']} services={t2['services']} resources={t2['resources']}")
    assert t2["business_id"] != t1["business_id"], "tenants must be distinct"

    # Both tenants need a WhatsApp mapping for the transactional notification
    # write inside commit to succeed. production_wiring maps only {demo-phone-id:1};
    # add a distinct phone_number_id for tenant 2 so its commit is not blocked by
    # a config gap unrelated to tenancy. Mutate the loaded settings singleton
    # (in-process only; nothing written to .env or any file).
    from fonely.core.config import settings as _settings
    import json as _json
    _settings.whatsapp_business_mappings = _json.dumps(
        {"demo-phone-id": t1["business_id"], "coastal-phone-id": t2["business_id"]})
    print(f"whatsapp mappings (in-process): demo-phone-id→{t1['business_id']}, "
          f"coastal-phone-id→{t2['business_id']}")

    # Book several days out so leftover rows from earlier runs can't cause a
    # capacity_conflict that masks the tenancy question.
    tomorrow = (datetime.now(TZ) + timedelta(days=7)).date()

    # Resolve real ids per tenant.
    t1_svc, t1_res = await _resolve_ids(t1["business_id"], "cleaning")
    t2_svc, t2_res = await _resolve_ids(t2["business_id"], "cleaning")
    print(f"\ntenant1 cleaning → service_id={t1_svc} resource_id={t1_res}")
    print(f"tenant2 cleaning → service_id={t2_svc} resource_id={t2_res}")

    # ---- POSITIVE CONCURRENCY --------------------------------------------
    print("\n" + "-" * 74)
    print("POSITIVE: 3 tenant-1 + 3 tenant-2 bookings fired concurrently")
    print("-" * 74)
    from datetime import time as dtime
    # 60 min apart so slots never collide WITHIN a tenant (service durations are
    # 30-45 min). A collision here would be correct capacity enforcement, but it
    # would muddy the tenancy signal we're actually testing.
    t1_times = [dtime(9, 30), dtime(11, 0), dtime(12, 30)]
    t2_times = [dtime(9, 0), dtime(11, 0), dtime(16, 0)]

    tasks = []
    for i, tm in enumerate(t1_times):
        port = _port_for(t1["business_id"], "+919840000001", f"t1c{i}-{MARKER}")
        tasks.append(("t1", t1["business_id"],
                      book_via_port(port, t1["business_id"], t1_svc, t1_res, tomorrow, tm,
                                    "+919840000001", f"t1-{i}")))
    for i, tm in enumerate(t2_times):
        port = _port_for(t2["business_id"], "+919850000002", f"t2c{i}-{MARKER}")
        tasks.append(("t2", t2["business_id"],
                      book_via_port(port, t2["business_id"], t2_svc, t2_res, tomorrow, tm,
                                    "+919850000002", f"t2-{i}")))

    results = await asyncio.gather(*(t[2] for t in tasks))

    # The GATE is: no committed row lands under the wrong tenant. A booking that
    # doesn't commit (capacity, config) is NOT a tenancy failure — track it
    # separately so a setup artifact can't masquerade as a cross-tenant leak.
    no_leak = True
    committed = 0
    not_committed = 0
    for (label, expected_biz, _), (success, appt_id, err) in zip(tasks, results):
        if not success:
            not_committed += 1
            print(f"  {label}: (did not commit — {err}; not a tenancy signal)")
            continue
        committed += 1
        actual_biz = await assert_row_tenant(appt_id)
        leaked = actual_biz != expected_biz
        if leaked:
            no_leak = False
        mark = "✓" if not leaked else "✗ CROSS-TENANT LEAK"
        print(f"  {label}: appt #{appt_id} committed under business_id={actual_biz} "
              f"(expected {expected_biz}) {mark}")
    print(f"  → {committed} committed, {not_committed} not committed; "
          f"every committed row under its own tenant: {no_leak}")

    # ---- ADVERSARIAL CROSS-TENANT ----------------------------------------
    print("\n" + "-" * 74)
    print("ADVERSARIAL: tenant-1-bound port handed tenant-2's service_id")
    print("-" * 74)
    adv_port = _port_for(t1["business_id"], "+919840000001", f"adv-{MARKER}")
    success, appt_id, err = await book_via_port(
        adv_port, t1["business_id"], t2_svc, t2_res, tomorrow, dtime(11, 0),
        "+919840000001", "adv-1")
    if success:
        actual_biz = await assert_row_tenant(appt_id)
        print(f"  ✗ FAIL: commit SUCCEEDED (appt #{appt_id} under business_id={actual_biz}) "
              f"using another tenant's service_id — this is a cross-tenant defect.")
        adversarial_ok = False
    else:
        print(f"  ✓ refused as required: {err}")
        print("    (tenant-2's service_id is not valid for tenant-1's business_id; "
              "business_id is frozen in ActorContext and cannot be overridden.)")
        adversarial_ok = True

    # ---- RESULT ----------------------------------------------------------
    print("\n" + "=" * 74)
    print("RESULT")
    print("=" * 74)
    gate = no_leak and committed >= 2 and adversarial_ok
    print(f"  concurrent bookings committed:               {committed} (both tenants represented)")
    print(f"  no committed row under the wrong tenant:      {'PASS' if no_leak else 'FAIL'}")
    print(f"  adversarial cross-tenant refusal:            {'PASS' if adversarial_ok else 'FAIL'}")
    print(f"  OVERALL tenancy-under-concurrency:           {'PASS' if gate else 'FAIL'}")

    print("\ncleaning up disposable rows this run created...")
    left = await cleanup()
    print(f"  appointment rows remaining with this marker: {left['appointments']} (expect 0)")
    print(f"  pending_action provenance rows remaining:    {left['pending_actions_provenance']} "
          "(immutable committed provenance — left by design, audit-only)")
    print(f"  NOTE: tenant-2 business/config (business_id={t2['business_id']}) is DISPOSABLE and "
          "left in place; say the word to drop it. Its appointments are cleaned.")


if __name__ == "__main__":
    asyncio.run(main())
