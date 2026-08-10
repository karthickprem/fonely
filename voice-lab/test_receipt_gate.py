"""Contract-conformance tests for the voice-owned receipt-aware TTS gate.

All receipts here are test-only fakes of the Architect-approved application
seam. They are not business engines and are impossible to configure as a live
application port.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

LAB_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(LAB_DIR))

from pipecat.frames.frames import (
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.tests.utils import run_test

from processors import BOOKING_FAILURE_RESPONSE, ReceiptAwareTTSGate


def _receipt(**overrides):
    now = datetime.now(UTC)
    receipt = {
        "status": "committed",
        "source": "booking_application",
        "operation": "create",
        "business_id": 1,
        "actor_id": "actor-1",
        "proposal_id": 7,
        "proposal_version": 3,
        "confirmation_id": "confirmation-1",
        "appointment_id": 11,
        "committed_at": now,
        "service_name": "Scaling",
        "resource_name": "Dr. Priya",
        "start_at_utc": datetime(2026, 8, 10, 11, 30, tzinfo=UTC),
        "end_at_utc": datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
        "business_timezone": "Asia/Kolkata",
        "customer_subject": "customer-1",
        "payload_digest": "digest-1",
        "notification_intent_state": "queued",
    }
    receipt.update(overrides)
    return receipt


async def _render(provider):
    gate = ReceiptAwareTTSGate(provider, business_id=1)
    frames, _ = await run_test(
        gate,
        frames_to_send=[
            LLMFullResponseStartFrame(),
            LLMTextFrame(text="Booking confirm ஆயிடுச்சு."),
            LLMFullResponseEndFrame(),
        ],
    )
    return "".join(frame.text for frame in frames if isinstance(frame, LLMTextFrame))


def test_success_wording_uses_only_receipt_facts_and_reads_once():
    class Provider:
        def __init__(self):
            self.calls = 0

        def __call__(self):
            self.calls += 1
            return _receipt()

    provider = Provider()
    spoken = asyncio.run(_render(provider))
    assert spoken == "Scaling appointment, Dr. Priya கிட்ட 10 August 2026 5:00 PM confirm ஆயிடுச்சு."
    assert provider.calls == 1


def test_no_port_blocks_success():
    assert asyncio.run(_render(None)) == BOOKING_FAILURE_RESPONSE


def test_application_error_blocks_success():
    def failed():
        raise RuntimeError("application failed")

    assert asyncio.run(_render(failed)) == BOOKING_FAILURE_RESPONSE


def test_timeout_blocks_success():
    async def timed_out():
        raise TimeoutError

    assert asyncio.run(_render(timed_out)) == BOOKING_FAILURE_RESPONSE


def test_conflict_or_noncommitted_result_blocks_success():
    assert asyncio.run(_render(lambda: _receipt(status="conflict"))) == BOOKING_FAILURE_RESPONSE


def test_stale_receipt_blocks_success():
    stale = datetime.now(UTC) - timedelta(minutes=10)
    assert asyncio.run(_render(lambda: _receipt(committed_at=stale))) == BOOKING_FAILURE_RESPONSE


def test_wrong_tenant_or_source_blocks_success():
    assert asyncio.run(_render(lambda: _receipt(business_id=2))) == BOOKING_FAILURE_RESPONSE
    assert asyncio.run(_render(lambda: _receipt(source="test_fake"))) == BOOKING_FAILURE_RESPONSE


def test_missing_receipt_identity_blocks_success():
    assert asyncio.run(_render(lambda: _receipt(confirmation_id=""))) == BOOKING_FAILURE_RESPONSE
    assert asyncio.run(_render(lambda: _receipt(payload_digest=""))) == BOOKING_FAILURE_RESPONSE
    assert asyncio.run(_render(lambda: _receipt(proposal_id=None))) == BOOKING_FAILURE_RESPONSE


def test_nonconsequential_speech_passes_without_receipt():
    async def run():
        gate = ReceiptAwareTTSGate(None, business_id=1)
        frames, _ = await run_test(
            gate,
            frames_to_send=[
                LLMFullResponseStartFrame(),
                LLMTextFrame(text="பேரு சொல்லுங்க?"),
                LLMFullResponseEndFrame(),
            ],
        )
        return "".join(frame.text for frame in frames if isinstance(frame, LLMTextFrame))

    assert asyncio.run(run()) == "பேரு சொல்லுங்க?"
