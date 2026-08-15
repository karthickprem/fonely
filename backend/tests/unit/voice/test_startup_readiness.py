"""#45(d): a voice-enabled process fails CLOSED when it cannot serve calls.

The gap: the composition builds providers PER CALL (lazily), so a voice-enabled
process with missing SELECTED-provider credentials constructs a runtime that
looks fine at startup and fails on the FIRST REAL CALL — silently serving dead
calls. The fix gates STARTUP (raise → fail-to-start) and READINESS (503) on the
ACTUALLY-SELECTED providers (resolved_voice_creds_ready, NOT the hardcoded
credentials_ready snapshot), and leaves a flag-OFF process byte-for-byte
unchanged.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from fonely.voice.config import LLMConfig, STTConfig, TTSConfig
from fonely.voice.providers import VoiceCredentialsError, resolved_voice_creds_ready

# STT=sarvam, TTS=cartesia (config defaults), LLM=anthropic (config default).
_CREDS_PRESENT = {
    "SARVAM_API_KEY": "s" * 10,
    "CARTESIA_API_KEY": "c" * 10,
    "CARTESIA_VOICE_ID": "v" * 10,
    "ANTHROPIC_API_KEY": "a" * 10,
}


class TestResolvedGateFailsClosed:
    def test_all_selected_creds_present_does_not_raise(self):
        resolved_voice_creds_ready(
            STTConfig(), TTSConfig(), LLMConfig(), environ=dict(_CREDS_PRESENT)
        )  # no raise

    def test_missing_stt_key_raises(self):
        env = dict(_CREDS_PRESENT)
        del env["SARVAM_API_KEY"]
        with pytest.raises(VoiceCredentialsError, match="STT provider 'sarvam'"):
            resolved_voice_creds_ready(STTConfig(), TTSConfig(), LLMConfig(), environ=env)

    def test_missing_tts_key_raises(self):
        env = dict(_CREDS_PRESENT)
        del env["CARTESIA_VOICE_ID"]
        with pytest.raises(VoiceCredentialsError, match="TTS provider 'cartesia'"):
            resolved_voice_creds_ready(STTConfig(), TTSConfig(), LLMConfig(), environ=env)

    def test_missing_llm_key_raises(self):
        # LLM default provider is anthropic; without ANTHROPIC_API_KEY the
        # resolved validator raises (an LLMConfigError, a RuntimeError subclass).
        env = dict(_CREDS_PRESENT)
        del env["ANTHROPIC_API_KEY"]
        with pytest.raises(RuntimeError):
            resolved_voice_creds_ready(STTConfig(), TTSConfig(), LLMConfig(), environ=env)

    def test_gate_does_not_require_anthropic_for_a_luna_deploy(self):
        # The false-gate fix: with the LLM selecting openai_compatible (Luna),
        # ANTHROPIC_API_KEY being UNSET must NOT fail the gate — only the SELECTED
        # provider's config matters.
        env = {
            "SARVAM_API_KEY": "s" * 10,
            "CARTESIA_API_KEY": "c" * 10,
            "CARTESIA_VOICE_ID": "v" * 10,
            "LUNA_KEY": "l" * 10,
            # ANTHROPIC_API_KEY deliberately absent
        }
        llm = LLMConfig(
            provider="openai_compatible",
            api_key_env="LUNA_KEY",
            base_url="https://llm-api.amd.com",
        )
        resolved_voice_creds_ready(STTConfig(), TTSConfig(), llm, environ=env)  # no raise

    def test_unknown_stt_provider_fails_closed(self):
        # An unknown provider string has no credential-probe entry → the gate
        # fails closed (can't prove serviceability → don't start), NOT a silent
        # pass. This is the map-defaults-to-raise proof.
        bad_stt = STTConfig(provider="mystery-stt")
        env = dict(_CREDS_PRESENT)
        with pytest.raises(VoiceCredentialsError, match="unknown STT provider 'mystery-stt'"):
            resolved_voice_creds_ready(bad_stt, TTSConfig(), LLMConfig(), environ=env)

    def test_unknown_tts_provider_fails_closed(self):
        bad_tts = TTSConfig(provider="mystery-tts")
        env = dict(_CREDS_PRESENT)
        with pytest.raises(VoiceCredentialsError, match="unknown TTS provider 'mystery-tts'"):
            resolved_voice_creds_ready(STTConfig(), bad_tts, LLMConfig(), environ=env)


class TestStartupFailsClosed:
    """The lifespan raises when voice is enabled but the SELECTED providers are
    not serviceable — a raise before yield = the app FAILS TO START = no
    silent-serving process."""

    @pytest.mark.asyncio
    async def test_voice_enabled_missing_creds_lifespan_raises(self):
        from fastapi import FastAPI

        from fonely import app as app_mod

        app = FastAPI()
        with (
            patch.object(app_mod, "settings") as s,
            patch.object(app_mod, "create_async_engine"),
            patch.object(app_mod, "async_sessionmaker"),
            patch.object(
                app_mod,
                "_build_voice_audio_runtime",
                side_effect=AssertionError("must not be reached when creds fail"),
            ),
            patch(
                "fonely.voice.providers.resolved_voice_creds_ready",
                side_effect=VoiceCredentialsError("creds missing"),
            ),
        ):
            s.voice_pipeline_enabled = True
            s.sarvam_api_key = ""
            s.log_format = "json"
            s.log_level = "INFO"
            s.host = "h"
            s.port = 1
            s.database_url = "postgresql+asyncpg://localhost/test"
            s.db_pool_size = 1
            s.db_max_overflow = 0
            s.db_pool_timeout = 1
            s.db_pool_recycle = 1
            # Entering the lifespan runs the fail-closed gate; it must RAISE (the
            # app never reaches `yield`, i.e. never starts serving).
            with pytest.raises(VoiceCredentialsError, match="creds missing"):
                async with app_mod.lifespan(app):
                    pass

    @pytest.mark.asyncio
    async def test_mutation_without_the_gate_a_broken_voice_process_would_start(self):
        # Mutation proof: if the fail-closed gate did NOT run, a voice-enabled
        # process with missing creds WOULD start (reach yield). We simulate "gate
        # removed" by patching it to a no-op and confirm startup then proceeds —
        # proving the raise in the real path is what stops the broken start.
        from fastapi import FastAPI

        from fonely import app as app_mod

        app = FastAPI()
        started = []
        with (
            patch.object(app_mod, "settings") as s,
            patch.object(app_mod, "create_async_engine"),
            patch.object(app_mod, "async_sessionmaker"),
            patch.object(app_mod, "_build_voice_audio_runtime", return_value=object()),
            patch("fonely.voice.providers.resolved_voice_creds_ready", return_value=None),
        ):
            s.voice_pipeline_enabled = True
            s.sarvam_api_key = ""
            s.log_format = "json"
            s.log_level = "INFO"
            s.host = "h"
            s.port = 1
            s.database_url = "postgresql+asyncpg://localhost/test"
            s.db_pool_size = 1
            s.db_max_overflow = 0
            s.db_pool_timeout = 1
            s.db_pool_recycle = 1
            async with app_mod.lifespan(app):
                started.append(True)  # reached yield → started
        assert started == [True]  # with the gate no-op'd, a broken voice start proceeds


class TestFlagOffUnaffected:
    """THE invariant: the whole additive/default-dark guarantee. A flag-OFF
    process runs NONE of the new voice fail-closed logic — byte-for-byte the old
    behavior."""

    @pytest.mark.asyncio
    async def test_flag_off_runs_no_voice_gate_and_starts_clean(self):
        from fastapi import FastAPI

        from fonely import app as app_mod

        app = FastAPI()
        with (
            patch.object(app_mod, "settings") as s,
            patch.object(app_mod, "create_async_engine"),
            patch.object(app_mod, "async_sessionmaker"),
            patch(
                "fonely.voice.providers.resolved_voice_creds_ready",
                side_effect=AssertionError("resolved_voice_creds_ready called when flag OFF"),
            ),
            patch(
                "fonely.voice.llm_selection.validate_llm_startup",
                side_effect=AssertionError("validate_llm_startup called when flag OFF"),
            ),
            patch.object(
                app_mod,
                "_build_voice_audio_runtime",
                side_effect=AssertionError("runtime built when flag OFF"),
            ),
        ):
            s.voice_pipeline_enabled = False  # DARK
            s.sarvam_api_key = ""
            s.log_format = "json"
            s.log_level = "INFO"
            s.host = "h"
            s.port = 1
            s.database_url = "postgresql+asyncpg://localhost/test"
            s.db_pool_size = 1
            s.db_max_overflow = 0
            s.db_pool_timeout = 1
            s.db_pool_recycle = 1
            # Starts clean: none of the voice gates/build ran (they'd AssertionError),
            # and the runtime is left None (dark).
            async with app_mod.lifespan(app):
                assert app.state.voice_audio_runtime is None


class _FakeConn:
    """A DB connection whose SELECT 1 succeeds — isolates the readiness test to
    the VOICE branch (the DB half is healthy, so a 503 can only come from voice)."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _stmt):
        return None


class _FakeEngine:
    def connect(self):
        return _FakeConn()


def _readiness_client(*, voice_enabled: bool, runtime: object | None):
    """A TestClient over create_app() with app.state wired by hand and the
    lifespan NOT run (TestClient runs lifespan only as a context manager). So the
    DB is a healthy fake and the ONLY variable under test is the voice branch."""
    from fastapi.testclient import TestClient

    from fonely import app as app_mod

    with patch.object(app_mod, "settings") as s:
        # create_app reads these router gates; keep them empty so no router group
        # mounts (irrelevant to /health/ready, and avoids importing channels).
        s.internal_api_secret = ""
        s.whatsapp_verify_token = ""
        s.exotel_webhook_secret = ""
        app = app_mod.create_app()
    app.state.engine = _FakeEngine()
    app.state.voice_audio_runtime = runtime

    # The endpoint reads settings live per request; patch for the call window.
    ctx = patch.object(app_mod, "settings")
    s = ctx.start()
    s.readiness_timeout_seconds = 3.0
    s.voice_pipeline_enabled = voice_enabled
    client = TestClient(app)
    client._readiness_settings_ctx = ctx  # keep alive; stopped by caller
    return client


class TestReadinessFailsClosed:
    """Defense-in-depth: a voice-ENABLED process that can't serve calls (runtime
    unmounted, or selected creds gone) must report 503, not green-light itself."""

    def test_voice_enabled_runtime_none_returns_503(self):
        client = _readiness_client(voice_enabled=True, runtime=None)
        try:
            # Even with creds present, a missing runtime alone must 503.
            with patch("fonely.voice.providers.resolved_voice_creds_ready", return_value=None):
                r = client.get("/health/ready")
            assert r.status_code == 503
        finally:
            client._readiness_settings_ctx.stop()

    def test_voice_enabled_creds_gone_returns_503(self):
        # Runtime mounted but SELECTED-provider creds rotated away mid-run → 503.
        client = _readiness_client(voice_enabled=True, runtime=object())
        try:
            with patch(
                "fonely.voice.providers.resolved_voice_creds_ready",
                side_effect=VoiceCredentialsError("creds gone"),
            ):
                r = client.get("/health/ready")
            assert r.status_code == 503
        finally:
            client._readiness_settings_ctx.stop()

    def test_voice_enabled_serviceable_returns_200(self):
        client = _readiness_client(voice_enabled=True, runtime=object())
        try:
            with patch("fonely.voice.providers.resolved_voice_creds_ready", return_value=None):
                r = client.get("/health/ready")
            assert r.status_code == 200
        finally:
            client._readiness_settings_ctx.stop()

    def test_mutation_flag_off_skips_voice_branch_db_only_200(self):
        # THE flag-off invariant at the readiness endpoint: flag OFF → the voice
        # branch is never consulted (patch resolved_voice_creds_ready to RAISE if
        # called), runtime is None, yet /health/ready is 200 on the DB check
        # alone. If the endpoint's voice check were NOT guarded by the flag, a
        # None runtime would 503 here — so this is also the mutation proof that
        # the `if settings.voice_pipeline_enabled:` guard is load-bearing.
        client = _readiness_client(voice_enabled=False, runtime=None)
        try:
            with patch(
                "fonely.voice.providers.resolved_voice_creds_ready",
                side_effect=AssertionError("voice creds checked when flag OFF"),
            ):
                r = client.get("/health/ready")
            assert r.status_code == 200
        finally:
            client._readiness_settings_ctx.stop()
