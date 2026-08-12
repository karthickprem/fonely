"""Interactive single-turn caller for Luna. Call with a message, get response."""
import os, json, httpx, sys

with open('/scratch/karthick/.claude/settings.json') as f:
    data = json.load(f)
    for k, v in data.get('env', {}).items():
        os.environ.setdefault(k, v)

base_url = os.environ.get('ANTHROPIC_BASE_URL', '')
sub_key = ''
for line in os.environ.get('ANTHROPIC_CUSTOM_HEADERS', '').split('\n'):
    if 'Ocp-Apim-Subscription-Key' in line:
        sub_key = line.split(':', 1)[1].strip()

SYSTEM = """You are Fonely, the virtual receptionist for Smile Dental Clinic in Aminjikarai, Chennai.

Speak like a warm local Chennai person, not a formal Tamil announcer or chatbot.
- Match the caller's Tamil, Tanglish, or Indian English.

Medical safety — follow strictly:
- Never suggest specific treatments, medications, dosages, or diagnoses.
- For pain or symptoms: acknowledge briefly, then refer to the clinic or doctor directly.

Booking collection — follow this exact order:
- Collection order: reason/service → date → time (from offered slots only) → patient name.
- Do NOT ask for name before date. Do NOT ask for phone number during collection.
- Do NOT offer availability or slots until the caller states a date.
- Once all four fields are collected, read back ALL facts and ask "இது correct-ஆ?"

Current context:
- Today is Monday, August 10, 2026.
- Business timezone: Asia/Kolkata.

Available slots for today (August 10):
  Dr. Priya: 10:00, 11:00, 17:00, 18:30 (scaling/consultation)

Available slots for tomorrow (August 11):
  Dr. Priya: 10:00, 11:00, 17:00, 18:30 (scaling/consultation)

Clinic: Dr. Priya, Mon-Sat, scaling Rs800, consultation Rs300, root canal Rs3500-5500. Sunday closed."""

# Load or init conversation history
HIST_FILE = '/tmp/luna-conv-history.json'
if os.path.exists(HIST_FILE):
    history = json.loads(open(HIST_FILE).read())
else:
    history = []

caller_text = sys.argv[1] if len(sys.argv) > 1 else ""
if not caller_text:
    print("Usage: python luna-interactive.py 'your message'")
    print("       python luna-interactive.py RESET  (start new conversation)")
    sys.exit(0)

if caller_text == "RESET":
    history = []
    open(HIST_FILE, 'w').write('[]')
    print("Conversation reset.")
    sys.exit(0)

history.append({"role": "user", "content": caller_text})

r = httpx.post(
    f"{base_url}/v1/chat/completions",
    headers={"Ocp-Apim-Subscription-Key": sub_key, "Content-Type": "application/json", "user": "karthick"},
    json={"model": "gpt-5.6-luna", "max_completion_tokens": 300,
          "messages": [{"role": "system", "content": SYSTEM}] + history},
    timeout=30,
)
if r.status_code != 200:
    print(f"ERROR: {r.status_code} {r.text[:200]}")
    sys.exit(1)

response = r.json()["choices"][0]["message"]["content"]
history.append({"role": "assistant", "content": response})
open(HIST_FILE, 'w').write(json.dumps(history, ensure_ascii=False))

print(f"LUNA: {response}")
