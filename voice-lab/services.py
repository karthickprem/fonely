"""Voice-lab provider adapters that preserve Pipecat frame semantics."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

import aiohttp

from pipecat.frames.frames import ErrorFrame, Frame, TTSAudioRawFrame
from pipecat.services.sarvam._sdk import sdk_headers
from pipecat.services.sarvam.tts import SarvamHttpTTSService
from pipecat.services.tts_service import TextAggregationMode
from pipecat.transcriptions.language import Language
from pipecat.utils.tracing.service_decorators import traced_tts


class SarvamStreamingHttpTTSService(SarvamHttpTTSService):
    """Sarvam HTTP-streaming TTS exposed as a Pipecat TTS service.

    Pipecat 1.7's Sarvam WebSocket adapter hard-codes linear16 and the current
    provider rejects that WebSocket config. Sarvam's authenticated HTTP stream
    returns the same raw PCM incrementally, so this adapter keeps Pipecat's TTS
    contexts, interruption handling, metrics, and WebRTC output while using the
    verified provider transport.
    """

    def __init__(
        self,
        *,
        api_key: str,
        aiohttp_session: aiohttp.ClientSession,
        model: str = "bulbul:v3",
        voice: str = "priya",
        language: Language = Language.TA_IN,
        pace: float = 0.95,
        temperature: float = 0.55,
        sample_rate: int = 24000,
        **kwargs,
    ):
        super().__init__(
            api_key=api_key,
            aiohttp_session=aiohttp_session,
            sample_rate=sample_rate,
            text_aggregation_mode=TextAggregationMode.SENTENCE,
            settings=self.Settings(
                model=model,
                voice=voice,
                language=language,
                pace=pace,
                temperature=temperature,
                enable_preprocessing=True,
            ),
            **kwargs,
        )
        self._stream_url = f"{self._base_url}/text-to-speech/stream"

    @traced_tts
    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame | None, None]:
        payload = {
            "text": text,
            "target_language_code": self._settings.language,
            "speaker": self._settings.voice,
            "model": self._settings.model,
            "output_audio_codec": "linear16",
            "pace": self._settings.pace,
            "temperature": self._settings.temperature,
        }
        headers = {
            "api-subscription-key": self._api_key,
            "Content-Type": "application/json",
            **sdk_headers(),
        }

        try:
            await self.start_ttfb_metrics()
            async with self._session.post(
                self._stream_url, json=payload, headers=headers
            ) as response:
                if response.status != 200:
                    body = await response.text()
                    yield ErrorFrame(error=f"Sarvam streaming TTS HTTP {response.status}: {body}")
                    return

                first_chunk = True
                audio_bytes = 0

                async def chunks():
                    nonlocal first_chunk, audio_bytes
                    async for chunk in response.content.iter_chunked(self.chunk_size):
                        if not chunk:
                            continue
                        audio_bytes += len(chunk)
                        if first_chunk:
                            first_chunk = False
                            await self.stop_ttfb_metrics()
                        yield bytes(chunk)

                async for frame in self._stream_audio_frames_from_iterator(
                    chunks(),
                    # Bulbul v3 HTTP streaming defaults to 24 kHz. The endpoint
                    # rejects speech_sample_rate in this transport.
                    in_sample_rate=24000,
                    context_id=context_id,
                ):
                    frame.context_id = context_id
                    yield frame

                if audio_bytes == 0:
                    yield ErrorFrame(error="Sarvam streaming TTS returned no audio")
                    return

            await self.start_tts_usage_metrics(text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            yield ErrorFrame(error=f"Sarvam streaming TTS failed: {exc}", exception=exc)
        finally:
            await self.stop_ttfb_metrics()
