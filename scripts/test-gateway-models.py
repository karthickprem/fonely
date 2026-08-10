"""Check which models the AMD gateway supports."""
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

# Try listing models
for endpoint in ["/models", "/v1/models"]:
    try:
        r = httpx.get(
            f"{base_url}{endpoint}",
            headers={"Ocp-Apim-Subscription-Key": sub_key},
            timeout=15,
        )
        print(f"GET {endpoint}: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict) and "data" in data:
                for m in data["data"]:
                    print(f"  {m.get('id', m)}")
            else:
                print(f"  {r.text[:500]}")
        else:
            print(f"  {r.text[:200]}")
    except Exception as e:
        print(f"  Error: {e}")

# Try specific OpenAI models
print("\nProbing specific models:")
for model in ["gpt-5.6-luna", "gpt-4o", "gpt-4.1", "gpt-4.1-mini", "gpt-5.6-terra", "gpt-5.5"]:
    try:
        r = httpx.post(
            f"{base_url}/chat/completions",
            headers={
                "Ocp-Apim-Subscription-Key": sub_key,
                "Content-Type": "application/json",
            },
            json={"model": model, "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]},
            timeout=15,
        )
        status = "OK" if r.status_code == 200 else f"{r.status_code}"
        print(f"  {model}: {status}")
    except Exception as e:
        print(f"  {model}: {type(e).__name__}")
