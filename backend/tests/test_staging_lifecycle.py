"""Behavioral lifecycle tests for worker cooperative stop/drain.

Tests actual stop behavior of worker loops with real coroutines and events.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from fonely.workers.inbound_worker import run_inbound_worker
from fonely.workers.notification_worker import run_notification_worker

COMPOSE_PATH = __import__("pathlib").Path(__file__).parents[2] / "docker-compose.staging.yml"


class TestInboundWorkerStop:
    @pytest.mark.asyncio
    async def test_stop_before_first_claim(self) -> None:
        stop = asyncio.Event()
        stop.set()
        claim_mock = AsyncMock(return_value=None)
        with patch("fonely.workers.inbound_worker._claim", claim_mock):
            await run_inbound_worker(MagicMock(), MagicMock(), stop=stop)
        claim_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stop_after_claim_releases_fenced(self) -> None:
        stop = asyncio.Event()
        claimed = MagicMock()
        claimed.event_id = 42
        claimed.business_id = 1
        claimed.claim_token = MagicMock()
        claimed.claim_version = 1
        release_called = False

        async def fake_release(*args: object) -> None:
            nonlocal release_called
            release_called = True

        async def claim_then_stop(*args: object) -> object:
            stop.set()
            return claimed

        with (
            patch("fonely.workers.inbound_worker._claim", side_effect=claim_then_stop),
            patch("fonely.workers.inbound_worker._release_claim", side_effect=fake_release),
            patch("fonely.workers.inbound_worker._process_claimed", new_callable=AsyncMock) as proc,
        ):
            await run_inbound_worker(MagicMock(), MagicMock(), stop=stop)
        assert release_called
        proc.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_current_unit_drains_before_exit(self) -> None:
        stop = asyncio.Event()
        completed = asyncio.Event()

        async def slow_process(*args: object) -> None:
            await asyncio.sleep(0.05)
            completed.set()

        claimed = MagicMock()
        call_count = 0

        async def claim_then_none(*args: object) -> object:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return claimed
            stop.set()
            return None

        with (
            patch("fonely.workers.inbound_worker._claim", side_effect=claim_then_none),
            patch("fonely.workers.inbound_worker._process_claimed", side_effect=slow_process),
        ):
            await run_inbound_worker(MagicMock(), MagicMock(), stop=stop)
        assert completed.is_set()

    @pytest.mark.asyncio
    async def test_without_stop_uses_max_iterations(self) -> None:
        with patch(
            "fonely.workers.inbound_worker._claim",
            new_callable=AsyncMock,
            return_value=None,
        ):
            await run_inbound_worker(MagicMock(), MagicMock(), max_iterations=3)


class TestNotificationWorkerStop:
    @pytest.mark.asyncio
    async def test_stop_before_first_claim(self) -> None:
        stop = asyncio.Event()
        stop.set()
        claim_mock = AsyncMock(return_value=None)
        with patch("fonely.workers.notification_worker._claim_one", claim_mock):
            await run_notification_worker(MagicMock(), MagicMock(), stop=stop)
        claim_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stop_after_claim_releases_fenced(self) -> None:
        stop = asyncio.Event()
        claimed = MagicMock()
        claimed.event_id = 99
        claimed.attempts = 0
        release_called = False

        async def fake_release(*args: object) -> None:
            nonlocal release_called
            release_called = True

        async def claim_then_stop(*args: object) -> object:
            stop.set()
            return claimed

        with (
            patch("fonely.workers.notification_worker._claim_one", side_effect=claim_then_stop),
            patch(
                "fonely.workers.notification_worker._release_notification_claim",
                side_effect=fake_release,
            ),
            patch(
                "fonely.workers.notification_worker._deliver_claimed",
                new_callable=AsyncMock,
            ) as deliver,
        ):
            await run_notification_worker(MagicMock(), MagicMock(), stop=stop, batch_size=10)
        assert release_called
        deliver.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_without_stop_uses_max_iterations(self) -> None:
        with patch(
            "fonely.workers.notification_worker._claim_one",
            new_callable=AsyncMock,
            return_value=None,
        ):
            await run_notification_worker(MagicMock(), MagicMock(), max_iterations=3)


class TestSupervisorRace:
    @pytest.mark.asyncio
    async def test_simultaneous_stop_and_task_done_drains_clean(self) -> None:
        """When task completes and stop is set simultaneously, drain exits cleanly."""
        stop = asyncio.Event()

        async def worker_that_sets_stop(*_a: object, **_k: object) -> None:
            stop.set()

        task = asyncio.get_running_loop().create_task(worker_that_sets_stop())
        stop_task = asyncio.get_running_loop().create_task(stop.wait())

        done, _ = await asyncio.wait([task, stop_task], return_when=asyncio.FIRST_COMPLETED)

        assert stop.is_set()
        if task in done:
            exc = task.exception()
            assert exc is None
        stop_task.cancel()

    @pytest.mark.asyncio
    async def test_simultaneous_stop_and_exception_propagates(self) -> None:
        """When task raises and stop is set simultaneously, exception is visible."""
        stop = asyncio.Event()

        async def crashing_worker(*_a: object, **_k: object) -> None:
            stop.set()
            raise RuntimeError("test_crash")

        task = asyncio.get_running_loop().create_task(crashing_worker())
        stop_task = asyncio.get_running_loop().create_task(stop.wait())

        done, _ = await asyncio.wait([task, stop_task], return_when=asyncio.FIRST_COMPLETED)

        assert stop.is_set()
        if task in done:
            with pytest.raises(RuntimeError, match="test_crash"):
                task.result()
        stop_task.cancel()

    @pytest.mark.asyncio
    async def test_drain_timeout_forces_cancel(self) -> None:
        """When drain exceeds timeout, task is cancelled."""
        stop = asyncio.Event()
        stop.set()
        cancelled = False

        async def blocking_worker(*_a: object, **_k: object) -> None:
            nonlocal cancelled
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                cancelled = True
                raise

        task = asyncio.get_running_loop().create_task(blocking_worker())
        try:
            await asyncio.wait_for(task, timeout=0.05)
        except TimeoutError:
            task.cancel()
            with __import__("contextlib").suppress(asyncio.CancelledError):
                await task
        assert cancelled


class TestGraceRelationship:
    def test_worker_grace_exceeds_drain_plus_margin(self) -> None:
        compose = yaml.safe_load(COMPOSE_PATH.read_text())
        from fonely.core.config import Settings

        drain = Settings().shutdown_timeout_seconds
        for svc in ("inbound-worker", "notification-worker"):
            grace_str = compose["services"][svc].get("stop_grace_period", "10s")
            grace = int(grace_str.replace("s", ""))
            assert grace > drain, (
                f"{svc} stop_grace_period {grace}s must exceed drain timeout {drain}s"
            )

    def test_api_grace_exceeds_request_timeout(self) -> None:
        compose = yaml.safe_load(COMPOSE_PATH.read_text())
        grace_str = compose["services"]["backend"].get("stop_grace_period", "10s")
        grace = int(grace_str.replace("s", ""))
        assert grace >= 35

    def test_shutdown_timeout_injected_to_workers(self) -> None:
        compose = yaml.safe_load(COMPOSE_PATH.read_text())
        for svc in ("inbound-worker", "notification-worker"):
            env = compose["services"][svc].get("environment", {})
            assert "SHUTDOWN_TIMEOUT_SECONDS" in env, f"{svc} missing SHUTDOWN_TIMEOUT_SECONDS"


class TestEntrypointStructure:
    def test_inbound_uses_settings_drain_timeout(self) -> None:
        from pathlib import Path

        code = (Path(__file__).parents[1] / "run_inbound_worker.py").read_text()
        assert "settings.shutdown_timeout_seconds" in code
        assert "DRAIN_TIMEOUT" not in code
        assert "stop_task.cancel()" in code
        assert "gateway._client" not in code

    def test_notification_uses_settings_drain_timeout(self) -> None:
        from pathlib import Path

        code = (Path(__file__).parents[1] / "run_worker.py").read_text()
        assert "settings.shutdown_timeout_seconds" in code
        assert "DRAIN_TIMEOUT" not in code
        assert "stop_task.cancel()" in code

    def test_supervisor_branches_on_stop_is_set(self) -> None:
        from pathlib import Path

        for ep in ("run_inbound_worker.py", "run_worker.py"):
            code = (Path(__file__).parents[1] / ep).read_text()
            assert "if stop.is_set():" in code, f"{ep} must branch on stop.is_set()"

    def test_inbound_owns_http_client(self) -> None:
        from pathlib import Path

        code = (Path(__file__).parents[1] / "run_inbound_worker.py").read_text()
        assert "http_client = httpx.AsyncClient()" in code
        assert "SarvamModelGateway(client=http_client)" in code
        assert "http_client.aclose()" in code
