"""The concrete composition root for the canonical voice runtime.

``audio_runtime.py`` owns the ORCHESTRATION contract — ``handle_audio_session``,
the open-order sequencing, release-once. This module owns the CONCRETE wiring it
drives: turning an admitted ``(websocket, session, handoff)`` into the
``CallComponents`` the runtime runs. It is the typed, production analogue of the
lab's ``run_booking_bot`` — same shape, but identity comes only from the admitted
``AudioSession`` and the DPDP evidence goes to the real ``SqlDpdpEvidenceWriter``.

Kept separate from ``audio_runtime`` on purpose: this pulls in the concrete
Pipecat transport, the provider SDKs, and the pipeline assembly, so the
orchestration module stays free of those deps and unit-testable with fakes.
Everything heavy is imported INSIDE ``composition_root`` so importing this module
(e.g. for the app mount) does not drag the provider SDKs into module load.

The one production invariant that differs from the lab: the SQL evidence writer
UPDATEs an EXISTING ``calls`` row, and admission already wrote that row —
``session.call_id`` IS it. So there is no ``create_call_row`` stand-in here (the
lab needed one because its demo has no admission). ``build_notice_open_sequence``
is handed ``session.call_id`` directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from starlette.websockets import WebSocket

    from .audio_runtime import CallComponents, VoiceAudioRuntime
    from .media_stream_types import AudioSession, AudioStreamHandoff


# The greeting spoken after the DPDP notice completes. Plain spoken Tanglish,
# matching the notice register. The clinic name comes from the admitted session.
_GREETING = "வணக்கம், {clinic}-ல இருந்து Fonely பேசுறேன், appointment book பண்ண உதவி பண்ணலாம்"

_LOCALE = "ta-IN"

# The short line the caller hears if the notice/evidence open sequence fails —
# spoken before the call is torn down so it is never a silent dead socket.
_FAILURE_LINE = "மன்னிக்கவும், தொழில்நுட்ப சிக்கல். தயவுசெய்து clinic-ஐ நேரடியாக அழைக்கவும்."


def make_composition_root(
    runtime: VoiceAudioRuntime,
    *,
    session_factory: Callable[[], Any],
    system_prompt: str,
) -> Callable[[WebSocket, AudioSession, AudioStreamHandoff], CallComponents]:
    """Return the ``compose`` callable ``VoiceAudioRuntime`` is constructed with.

    Closes over the ``runtime`` (for its injected ``command_port`` +
    ``resolver_factory``), the DB ``session_factory`` (for the SQL evidence
    writer), and the ``system_prompt``. The returned callable takes exactly the
    transport-facing tuple ``(websocket, session, handoff)`` and builds the
    per-call ``CallComponents``.
    """

    def compose(
        websocket: WebSocket,
        session: AudioSession,
        handoff: AudioStreamHandoff,
    ) -> CallComponents:
        from datetime import UTC, datetime

        from pipecat.pipeline.runner import PipelineRunner
        from pipecat.pipeline.task import PipelineTask

        from .audio_runtime import CallComponents, build_transport
        from .config import LLMConfig, STTConfig, TTSConfig
        from .evidence import SqlDpdpEvidenceWriter
        from .input_latch import NoticeInputLatch
        from .notice_playback import build_notice_open_sequence
        from .pipeline_assembly import build_voice_pipeline
        from .playback_signal import NoticePlaybackSignal
        from .providers import build_llm, build_stt, build_tts
        from .session_open import open_session

        start = handoff.start

        # Transport + providers. The transport decode rate comes only from the
        # admitted start event (build_transport / build_serializer enforce that).
        transport = build_transport(websocket, start)
        stt = build_stt(STTConfig())
        llm = build_llm(LLMConfig())
        tts = build_tts(TTSConfig())

        input_latch = NoticeInputLatch()
        playback = NoticePlaybackSignal()

        # Resolver: business_id / tenant come ONLY from the admitted session.
        # The command port is built PER CALL from the admitted session
        # (build_call_resolver → command_port_factory → session.business_id), so
        # the sole commit route is tenant-bound by construction. Never a frame or
        # the model.
        resolver = runtime.build_call_resolver(session)

        # The playback signal observes the output (bot-stopped-speaking) so the
        # open order waits for real notice playback, not a guessed sleep — placed
        # after transport_out by build_voice_pipeline, mirroring the lab.
        assembled = build_voice_pipeline(
            resolver=resolver,
            transport_in=transport.input(),
            transport_out=transport.output(),
            stt=stt,
            llm=llm,
            tts=tts,
            input_latch=input_latch,
            system_prompt=system_prompt,
            playback_observer=playback,
        )

        task = PipelineTask(assembled.pipeline)
        runner = PipelineRunner(handle_sigint=False)

        opening = open_session(
            clinic_name=session.clinic_name,
            greeting_text=_GREETING.format(clinic=session.clinic_name),
            locale=_LOCALE,
        )

        # The SQL writer UPDATEs the dpdp_notice_* columns on session.call_id's
        # row — admission already wrote that row, so no create-row stand-in.
        evidence_writer = SqlDpdpEvidenceWriter(session_factory=session_factory)

        async def _queue_frames(frames: Sequence[object]) -> None:
            await task.queue_frames(frames)  # type: ignore[arg-type]

        def _make_speech_frames(text: str) -> Sequence[object]:
            from pipecat.frames.frames import (
                LLMFullResponseEndFrame,
                LLMFullResponseStartFrame,
                LLMTextFrame,
            )

            return [
                LLMFullResponseStartFrame(),
                LLMTextFrame(text=text),
                LLMFullResponseEndFrame(),
            ]

        async def _await_playback_complete() -> bool:
            return await playback.await_complete(timeout=30.0)

        open_sequence = build_notice_open_sequence(
            call_id=session.call_id,
            opening=opening,
            locale=_LOCALE,
            queue_frames=_queue_frames,
            make_speech_frames=_make_speech_frames,
            await_playback_complete=_await_playback_complete,
            evidence_writer=evidence_writer,
            latch=input_latch,
            now=lambda: datetime.now(UTC),
            failure_line=_FAILURE_LINE,
        )

        async def _teardown() -> None:
            await task.cancel()

        return CallComponents(
            input_latch=input_latch,
            open_sequence=open_sequence,
            teardown=_teardown,
            pipeline_task=task,
            runner=runner,
        )

    return compose


async def run_pipeline_runner(components: CallComponents) -> None:
    """Drive the Pipecat runner to completion for a composed call.

    This is the ``run_runner`` seam ``VoiceAudioRuntime`` is constructed with in
    production; it runs only after the open sequence returned OPENED (the runtime
    enforces that), so caller audio has already been permitted at the latch."""
    runner: Any = components.runner
    task: Any = components.pipeline_task
    await runner.run(task)
