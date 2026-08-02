"""Convert sanitized Roman-Tamil reply patterns into TTS-ready Tamil script."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from pathlib import Path

from anthropic import AsyncAnthropic
from dotenv import load_dotenv

SYSTEM = """Transliterate Chennai Tanglish for Tamil TTS.
Preserve meaning, warmth, word order, punctuation, and genuine English words exactly.
Convert only Romanized Tamil words to natural spoken Tamil script.
Do not rewrite, improve, add, remove, explain, or translate English words.
Return only valid JSON: an array of objects with id and agent_tts.
Example: [{"id":"x","agent_tts":"அய்யோ, சரிங்க. ரொம்ப severe pain-ஆ?"}]"""


def parse_json(text: str) -> list[dict]:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    value = json.loads(text)
    if not isinstance(value, list):
        raise ValueError("Expected a JSON array")
    return value


async def convert_batch(client: AsyncAnthropic, batch: list[dict]) -> list[dict]:
    payload = [{"id": item["id"], "agent": item["agent"]} for item in batch]
    response = await client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1600,
        temperature=0,
        system=SYSTEM,
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
    )
    text = "".join(block.text for block in response.content if block.type == "text")
    return parse_json(text)


async def main_async(source: Path, output: Path, batch_size: int) -> None:
    load_dotenv(source.parent.parent / ".env")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is required")

    payload = json.loads(source.read_text())
    examples = payload["examples"]
    converted: dict[str, str] = {
        example["id"]: example["agent_tts"]
        for example in examples
        if example.get("agent_tts")
    }
    pending = [example for example in examples if not example.get("agent_tts")]

    async with AsyncAnthropic(api_key=api_key) as client:
        for offset in range(0, len(pending), batch_size):
            batch = pending[offset : offset + batch_size]
            result = await convert_batch(client, batch)
            expected = {item["id"] for item in batch}
            received = {item.get("id") for item in result}
            if received != expected:
                raise ValueError(f"ID mismatch at batch {offset}: {expected ^ received}")
            for item in result:
                converted[item["id"]] = item["agent_tts"].strip()
            done = len(converted)
            print(f"Converted {done}/{len(examples)}")

    for example in examples:
        example["agent_tts"] = converted[example["id"]]
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(f"Wrote {len(examples)} TTS-ready examples to {output}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--batch-size", type=int, default=10)
    args = parser.parse_args()
    asyncio.run(main_async(args.source, args.output, args.batch_size))


if __name__ == "__main__":
    main()
