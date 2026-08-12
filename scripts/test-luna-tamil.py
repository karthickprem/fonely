"""Real Tamil booking conversations on GPT-5.6 Luna via AMD gateway."""
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
- This is a demo — bookings will not be saved.

Available slots for today:
  Dr. Priya: 10:00-10:30, 11:00-11:30, 17:00-17:30, 18:30-19:00 (scaling)

Clinic: Dr. Priya, Mon-Sat, scaling/consultation/root canal. Consultation Rs300, scaling Rs800."""

headers = {
    "Ocp-Apim-Subscription-Key": sub_key,
    "Content-Type": "application/json",
    "user": "karthick",
}

def chat(messages):
    body = {
        "model": "gpt-5.6-luna",
        "max_completion_tokens": 300,
        "messages": [{"role": "system", "content": SYSTEM}] + messages,
    }
    r = httpx.post(f"{base_url}/v1/chat/completions", headers=headers, json=body, timeout=30)
    if r.status_code != 200:
        return f"ERROR {r.status_code}: {r.text[:200]}"
    return r.json()["choices"][0]["message"]["content"]

def run_conversation(title, turns):
    print("=" * 60)
    print(title)
    print("=" * 60)
    messages = []
    for i, caller_text in enumerate(turns):
        messages.append({"role": "user", "content": caller_text})
        response = chat(messages)
        messages.append({"role": "assistant", "content": response})
        print(f"\nTurn {i+1}:")
        print(f"  CALLER: {caller_text}")
        print(f"  LUNA:   {response}")
    print()

# Conversation 1: Karthick's exact defect scenario
run_conversation("CONV 1: Karthick exact defect — date+time first", [
    "இன்னைக்கு எனக்கு 12 மணிக்கு appointment புக் பண்ணனும்.",
    "எனக்கு 05:00 மணிக்கு ஓகே.",
    "பல்லு வலிக்காக பல்லு சொத்தை. Chocolate சாப்டா.",
    "Karthick",
    "ஆமா",
])

# Conversation 2: Simple booking flow
run_conversation("CONV 2: Simple booking — reason first", [
    "Appointment book pannanum",
    "Scaling",
    "Naalaikku",
    "6:30",
    "Karthick",
])

# Conversation 3: Tamil-only speaker
run_conversation("CONV 3: Tamil-only speaker", [
    "டாக்டர் கிட்ட போகணும்",
    "பல்லு வலிக்குது",
    "நாளைக்கு",
    "மாலை 6:30",
    "முருகன்",
])
