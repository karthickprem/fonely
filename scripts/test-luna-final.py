"""Test GPT-5.6 Luna with correct parameters."""
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
    "max_completion_tokens": 50,
    "messages": [{"role": "user", "content": "Reply with just: hello"}],
}

r = httpx.post(f"{base_url}/v1/chat/completions", headers=headers, json=body, timeout=30)
print(f"Status: {r.status_code}")
if r.status_code == 200:
    data = r.json()
    content = data["choices"][0]["message"]["content"]
    print(f"Response: {content}")
    print(f"Model: {data.get('model', '?')}")
    print(f"Usage: {data.get('usage', {})}")
    print("GPT-5.6 LUNA: SUCCESS")
else:
    print(f"Error: {r.text[:500]}")
