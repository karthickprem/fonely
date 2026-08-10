"""Tests for provider adapters and audio frame buffer."""
from fonely.voice.adapters import AudioFrameBuffer, CartesiaTTSAdapter, SarvamSTTAdapter
from fonely.voice.config import STTConfig, TTSConfig


class TestAudioFrameBuffer:
    def test_accumulate_and_flush(self):
        buf = AudioFrameBuffer(max_frames=10)
        for i in range(5):
            buf.add_frame(b"\x00" * 320)
        assert buf.frame_count == 5
        assert buf.duration_ms == 100.0
        audio = buf.flush_utterance()
        assert len(audio) == 320 * 5
        assert buf.frame_count == 0

    def test_bounded_drops_oldest(self):
        buf = AudioFrameBuffer(max_frames=3)
        for i in range(5):
            buf.add_frame(bytes([i]) * 10)
        assert buf.frame_count == 3
        assert buf.dropped_frames == 2
        assert buf.total_frames == 5
        audio = buf.flush_utterance()
        assert audio[:10] == bytes([2]) * 10

    def test_clear(self):
        buf = AudioFrameBuffer()
        buf.add_frame(b"\x00" * 10)
        buf.clear()
        assert buf.frame_count == 0

    def test_stats(self):
        buf = AudioFrameBuffer(max_frames=100, frame_ms=20)
        for _ in range(10):
            buf.add_frame(b"\x00")
        stats = buf.stats()
        assert stats["buffered_frames"] == 10
        assert stats["buffered_ms"] == 200.0
        assert stats["total_frames"] == 10
        assert stats["dropped_frames"] == 0


class TestAdapterInitialization:
    def test_stt_adapter_fails_without_service(self):
        import asyncio
        adapter = SarvamSTTAdapter(STTConfig())
        try:
            asyncio.get_event_loop().run_until_complete(adapter.transcribe(b""))
            assert False, "should raise"
        except RuntimeError as e:
            assert "not initialized" in str(e)

    def test_tts_adapter_tracks_characters(self):
        import asyncio
        adapter = CartesiaTTSAdapter(TTSConfig())
        adapter.set_service(object())
        asyncio.get_event_loop().run_until_complete(adapter.synthesize("hello"))
        assert adapter.total_characters == 5
