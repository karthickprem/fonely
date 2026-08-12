"""Try different API paths for GPT-5.6 Luna on AMD gateway."""
import os, json, httpx

with open('/scratch/karthick/.claude/settings.json') as f:
    data = json.load(f)
    for k, v in data.get('env', {}).items():
        os.environ.setdefault(k, v)

base_url = os.environ.get('ANTHROPIC_BASE_URL', '')
sub_key = ''
for line in os.environ.get('ANTHROPIC_CUSTOM_HEADERS', '').split('\n'):
    if 'Ocp-Apim-Subscription-Key' in line:
        sub_key = line.split(':', 1)[1].strip()

headers = {
    "Ocp-Apim-Subscription-Key": sub_key,
    "Content-Type": "application/json",
    "user": "karthick",
}

body = {
    "model": "gpt-5.6-luna",
    "max_tokens": 50,
    "messages": [{"role": "user", "content": "Reply with just: hello"}],
}

paths = [
    "/chat/completions",
    "/v1/chat/completions",
    "/openai/chat/completions",
    "/v1/openai/chat/completions",
    "/openai/deployments/gpt-5.6-luna/chat/completions",
]

for path in paths:
    url = f"{base_url}{path}"
    try:
        r = httpx.post(url, headers=headers, json=body, timeout=15)
        print(f"POST {path}: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            print(f"  RESPONSE: {content}")
            print(f"  SUCCESS!")
            break
        else:
            print(f"  {r.text[:200]}")
    except Exception as e:
        print(f"  {type(e).__name__}: {e}")
