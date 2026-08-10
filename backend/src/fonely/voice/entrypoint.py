"""Provider-neutral voice runtime entrypoint.

Wires PipelineRuntime end-to-end through a typed MediaPort.
Production callers (WebRTC, WebSocket, test harness) implement
MediaPort to supply audio and receive synthesized output.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from .config import VoiceSessionConfig
from .context import AvailabilityPort, StubAvailabilityPort, TrustedClock
from .runtime import CommandPort, LLMPort, PipelineRuntime, STTPort, TTSPort

logger = logging.getLogger("fonely.voice.entrypoint")


class MediaPort(Protocol):
    """Typed media transport boundary.

    Production implementations: WebRTC track adapter, WebSocket
    audio stream, or test audio fixture feeder.
    """
    async def receive_audio(self) -> bytes | None:
        """Return next audio chunk, or None when caller disconnects."""
        ...

    async def send_audio(self, audio: bytes) -> None:
        """Send synthesized audio to the caller."""
        ...

    async def send_event(self, event: dict[str, Any]) -> None:
        """Send a structured event (greeting, status, terminal)."""
        ...

    async def close(self) -> None: ...


async def run_voice_session(
    config: VoiceSessionConfig,
    *,
    clock: TrustedClock,
    business_name: str,
    business_timezone: str,
    business_context: str = "",
    media: MediaPort,
    stt: STTPort,
    llm: LLMPort,
    tts: TTSPort,
    availability_port: AvailabilityPort | None = None,
    command_port: CommandPort | None = None,
    validator: Any = None,
    session_mode: str = "demo",
) -> dict[str, Any]:
    """Run one complete voice session through the pipeline runtime.

    This is the production entrypoint.  Returns session summary
    with usage, turns, and close reason.
    """
    runtime = PipelineRuntime(
        config,
        clock=clock,
        business_name=business_name,
        business_context=business_context,
        business_timezone=business_timezone,
        stt=stt,
        llm=llm,
        tts=tts,
        validator=validator,
        availability_port=availability_port or StubAvailabilityPort(),
        command_port=command_port,
        session_mode=session_mode,
    )

    try:
        await runtime.initialize()

        # Send greeting (single TTS call at entrypoint level)
        from .prompts import build_greeting
        greeting = build_greeting(business_name)
        greeting_audio = await tts.synthesize(greeting)
        await media.send_audio(greeting_audio)
        await media.send_event({"type": "greeting", "text_length": len(greeting)})

        # Turn loop — runtime.process_turn returns TurnResult with audio
        while True:
            audio = await media.receive_audio()
            if audio is None:
                break

            result = await runtime.process_turn(audio)

            # Send response audio exactly once from TurnResult
            if result.response_audio:
                await media.send_audio(result.response_audio)

            await media.send_event({
                "type": "turn_complete",
                "turn": result.turn_number,
                "allowed": result.allowed,
                "terminal": result.terminal,
                "speech_class": result.speech_class,
                "has_audio": len(result.response_audio) > 0,
                "commit_receipt": result.commit_receipt is not None,
            })

            if result.terminal:
                from .dialogue import get_terminal_response
                terminal_text = get_terminal_response(result.terminal_reason)
                if terminal_text:
                    terminal_audio = await tts.synthesize(terminal_text)
                    await media.send_audio(terminal_audio)
                await media.send_event({
                    "type": "session_terminal",
                    "reason": result.terminal_reason,
                })
                break

        return await runtime.close("normal")

    except asyncio.CancelledError:
        summary = await runtime.close("cancelled")
        raise  # Re-raise CancelledError after cleanup
    except Exception as exc:
        logger.error("session_error", extra={
            "session": config.session_id,
            "error": type(exc).__name__,
        })
        return await runtime.close(f"error:{type(exc).__name__}")
    finally:
        try:
            await media.close()
        except Exception:
            pass
