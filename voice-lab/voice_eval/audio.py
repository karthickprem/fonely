from __future__ import annotations

import hashlib
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soxr


@dataclass(frozen=True)
class AudioMetadata:
    sha256: str
    sample_rate_hz: int
    channels: int
    sample_width_bits: int
    duration_ms: int
    frames: int


def inspect_wav(path: Path) -> AudioMetadata:
    data = path.read_bytes()
    if not data.startswith(b"RIFF"):
        raise ValueError("Audio must be a RIFF WAV file")
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        width = wav.getsampwidth() * 8
        rate = wav.getframerate()
        frames = wav.getnframes()
        compression = wav.getcomptype()
    if compression != "NONE" or channels != 1 or width != 16:
        raise ValueError("Only uncompressed PCM16 mono WAV is supported")
    duration_ms = round(frames / rate * 1000)
    return AudioMetadata(hashlib.sha256(data).hexdigest(), rate, channels, width, duration_ms, frames)


def resolve_audio_path(data_root: Path, relative_path: str) -> Path:
    root = data_root.resolve(strict=True)
    path = (root / relative_path).resolve(strict=True)
    if not path.is_relative_to(root):
        raise ValueError("Audio path escapes the evaluation data root")
    return path


def verify_fixture_audio(fixture: dict, data_root: Path) -> AudioMetadata:
    path = resolve_audio_path(data_root, fixture["audio"]["relative_path"])
    actual = inspect_wav(path)
    declared = fixture["audio"]
    for key, value in {
        "sha256": actual.sha256,
        "sample_rate_hz": actual.sample_rate_hz,
        "channels": actual.channels,
        "sample_width_bits": actual.sample_width_bits,
        "duration_ms": actual.duration_ms,
    }.items():
        if declared[key] != value:
            raise ValueError(f"{fixture['fixture_id']} audio {key} mismatch: declared={declared[key]!r} actual={value!r}")
    return actual


def read_pcm16_mono(path: Path, target_rate: int = 16000) -> bytes:
    metadata = inspect_wav(path)
    with wave.open(str(path), "rb") as wav:
        audio = wav.readframes(wav.getnframes())
    if metadata.sample_rate_hz == target_rate:
        return audio
    samples = np.frombuffer(audio, dtype="<i2").astype(np.float32) / 32768.0
    converted = soxr.resample(samples, metadata.sample_rate_hz, target_rate, quality="HQ")
    return (np.clip(converted, -1.0, 1.0) * 32767).astype("<i2").tobytes()
