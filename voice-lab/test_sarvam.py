"""Phase 0 — Verify Sarvam API connectivity before building anything."""

import io
import json
import os
import struct
import wave

import dotenv

dotenv.load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

API_KEY = os.environ.get("SARVAM_API_KEY", "")
print(f"API Key present: {bool(API_KEY)}, length: {len(API_KEY)}")

# We use urllib to avoid needing requests as a dependency
import urllib.request
import urllib.error


def test_rest_stt():
    """Test A — REST STT with a generated WAV file."""
    print("\n=== Test A: REST STT ===")

    # Generate a 1-second WAV with a tone at 440Hz
    sample_rate = 16000
    duration = 1.0
    num_samples = int(sample_rate * duration)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        import math

        for i in range(num_samples):
            val = int(10000 * math.sin(2 * math.pi * 440 * i / sample_rate))
            wf.writeframes(struct.pack("<h", val))

    wav_data = buf.getvalue()
    print(f"Generated WAV: {len(wav_data)} bytes")

    # Multipart form upload
    boundary = "----FormBoundary7MA4YWxkTrZu0gW"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="test.wav"\r\n'
        f"Content-Type: audio/wav\r\n\r\n"
    ).encode() + wav_data + (
        f"\r\n--{boundary}\r\n"
        f'Content-Disposition: form-data; name="model"\r\n\r\n'
        f"saaras:v3\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="language_code"\r\n\r\n'
        f"unknown\r\n"
        f"--{boundary}--\r\n"
    ).encode()

    req = urllib.request.Request(
        "https://api.sarvam.ai/speech-to-text",
        data=body,
        headers={
            "api-subscription-key": API_KEY,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            print(f"Status: {resp.status}")
            print(f"Response: {json.dumps(result, indent=2)}")
            print("STT REST: WORKING")
            return True
    except urllib.error.HTTPError as e:
        print(f"HTTP Error: {e.code}")
        print(f"Body: {e.read().decode()[:500]}")
        return False
    except Exception as e:
        print(f"Error: {e}")
        return False


def test_rest_tts():
    """Test B — REST TTS."""
    print("\n=== Test B: REST TTS ===")

    payload = json.dumps(
        {
            "text": "வணக்கம், Smile Dental Clinic",
            "target_language_code": "ta-IN",
            "model": "bulbul:v3",
            "speaker": "kavitha",
            "speech_sample_rate": 24000,
            "output_audio_codec": "wav",
            "pace": 1.0,
        }
    ).encode()

    req = urllib.request.Request(
        "https://api.sarvam.ai/text-to-speech",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "api-subscription-key": API_KEY,
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            if result.get("audios"):
                import base64

                audio_bytes = base64.b64decode(result["audios"][0])
                outpath = os.path.join(os.path.dirname(__file__), "test_output.wav")
                with open(outpath, "wb") as f:
                    f.write(audio_bytes)
                magic = audio_bytes[:4]
                print(f"Status: {resp.status}")
                print(f"Audio bytes: {len(audio_bytes)}")
                print(f"First 4 bytes: {magic}")
                print(f"Format: {'WAV (RIFF)' if magic == b'RIFF' else 'raw PCM'}")
                print(f"Saved to: {outpath}")
                print("TTS REST: WORKING")
                return True
            else:
                print(f"No audios in response: {json.dumps(result)[:500]}")
                return False
    except urllib.error.HTTPError as e:
        print(f"HTTP Error: {e.code}")
        print(f"Body: {e.read().decode()[:500]}")
        return False
    except Exception as e:
        print(f"Error: {e}")
        return False


def test_llm():
    """Test C — LLM Chat."""
    print("\n=== Test C: LLM Chat ===")

    payload = json.dumps(
        {
            "model": "sarvam-105b",
            "messages": [{"role": "user", "content": "Say hello in Tamil in one short sentence"}],
            "max_tokens": 50,
        }
    ).encode()

    req = urllib.request.Request(
        "https://api.sarvam.ai/v1/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "api-subscription-key": API_KEY,
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            text = result["choices"][0]["message"]["content"]
            print(f"Status: {resp.status}")
            print(f"Response: {text}")
            print("LLM: WORKING")
            return True
    except urllib.error.HTTPError as e:
        print(f"HTTP Error: {e.code}")
        print(f"Body: {e.read().decode()[:500]}")
        return False
    except Exception as e:
        print(f"Error: {e}")
        return False


def test_pipecat_import():
    """Test D — Pipecat import."""
    print("\n=== Test D: Pipecat Import ===")
    try:
        from pipecat.services.sarvam.stt import SarvamSTTService  # noqa: F401

        print("SarvamSTTService: OK")
        from pipecat.services.sarvam.tts import SarvamTTSService  # noqa: F401

        print("SarvamTTSService: OK")
        from pipecat.services.sarvam.llm import SarvamLLMService  # noqa: F401

        print("SarvamLLMService: OK")
        print("Pipecat import: OK")
        return True
    except ImportError as e:
        print(f"Import failed: {e}")
        print("Pipecat import: FAILED")
        return False


if __name__ == "__main__":
    print("=== SARVAM API CONNECTIVITY TEST ===\n")

    results = {}
    results["stt"] = test_rest_stt()
    results["tts"] = test_rest_tts()
    results["llm"] = test_llm()
    results["pipecat"] = test_pipecat_import()

    print("\n=== SUMMARY ===")
    for k, v in results.items():
        print(f"  {k}: {'PASS' if v else 'FAIL'}")

    if all(results.values()):
        print("\nAll tests passed. Ready to build pipeline.")
    else:
        failed = [k for k, v in results.items() if not v]
        print(f"\nFailed: {', '.join(failed)}. Fix before proceeding.")
