"""Test GPT-5.6 Luna through AMD gateway."""
import os, json

with open('/scratch/karthick/.claude/settings.json') as f:
    data = json.load(f)
    for k, v in data.get('env', {}).items():
        os.environ.setdefault(k, v)

base_url = os.environ.get('ANTHROPIC_BASE_URL', '')
sub_key = ''
for line in os.environ.get('ANTHROPIC_CUSTOM_HEADERS', '').split('\n'):
    if 'Ocp-Apim-Subscription-Key' in line:
        sub_key = line.split(':', 1)[1].strip()

print(f"Gateway: {base_url}")
print(f"Key present: {len(sub_key) > 10}")

from openai import OpenAI

client = OpenAI(
    api_key=sub_key,
    base_url=base_url,
)

try:
    response = client.chat.completions.create(
        model="gpt-5.6-luna",
        max_tokens=100,
        messages=[{"role": "user", "content": "Reply with just: hello"}],
    )
    print(f"Response: {response.choices[0].message.content}")
    print("GPT-5.6 LUNA VIA AMD GATEWAY: SUCCESS")
except Exception as e:
    print(f"OpenAI SDK error: {type(e).__name__}: {e}")

    # Try raw HTTP with the gateway
    import httpx
    try:
        r = httpx.post(
            f"{base_url}/chat/completions",
            headers={
                "Ocp-Apim-Subscription-Key": sub_key,
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-5.6-luna",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "Reply with just: hello"}],
            },
            timeout=30,
        )
        print(f"Raw HTTP status: {r.status_code}")
        print(f"Raw response: {r.text[:500]}")
    except Exception as e2:
        print(f"Raw HTTP error: {e2}")
