"""Real-world dental clinic booking scenarios on GPT-5.6 Luna.

10+ scenarios covering what actual callers do — not clean lab inputs.
"""
import os, json, httpx, asyncio, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend" / "src"))

with open('/scratch/karthick/.claude/settings.json') as f:
    data = json.load(f)
    for k, v in data.get('env', {}).items():
        os.environ.setdefault(k, v)

from fonely.voice.llm_openai import OpenAILLMAdapter
from fonely.voice.config import VoiceSessionConfig
from fonely.voice.context import AvailableSlot, DayAvailability, TrustedClock
from fonely.voice.runtime import PipelineRuntime
from fonely.voice.dialogue import contains_medical_advice
from datetime import date, time, datetime, timezone

class TextSTT:
    async def transcribe(self, audio): return audio.decode('utf-8')
    async def close(self): pass

class TextTTS:
    def __init__(self): self.calls = 0
    async def synthesize(self, text): self.calls += 1; return text.encode()
    async def close(self): pass

class TestAvail:
    async def query_day_availability(self, q):
        return DayAvailability(
            business_date=q.target_date, day_of_week=q.target_date.strftime('%A').lower(),
            is_operating_day=q.target_date.weekday() < 6,
            is_exception_day=False,
            operating_hours=((time(10,0),time(13,0)),(time(17,0),time(20,30))) if q.target_date.weekday() < 6 else (),
            available_slots=(
                AvailableSlot(1, 'Dr. Priya', time(10,0), time(10,30), 'consultation'),
                AvailableSlot(1, 'Dr. Priya', time(11,0), time(11,30), 'scaling'),
                AvailableSlot(1, 'Dr. Priya', time(17,0), time(17,30), 'consultation'),
                AvailableSlot(1, 'Dr. Priya', time(18,30), time(19,0), 'scaling'),
            ) if q.target_date.weekday() < 6 else ())

def make_runtime():
    return PipelineRuntime(
        VoiceSessionConfig(session_id=f'luna-{id(object())}', business_id=1),
        clock=TrustedClock.from_now('Asia/Kolkata'),
        business_name='Smile Dental Clinic',
        business_context='Dr. Priya: Mon-Sat, scaling Rs800, consultation Rs300, root canal Rs3500-5500.',
        business_timezone='Asia/Kolkata',
        stt=TextSTT(), llm=OpenAILLMAdapter(), tts=TextTTS(),
        availability_port=TestAvail(), session_mode='demo',
    )

async def run_scenario(title, turns):
    print('=' * 60)
    print(f'SCENARIO: {title}')
    print('=' * 60)
    rt = make_runtime()
    await rt.initialize()
    defects = []
    for i, caller in enumerate(turns):
        result = await rt.process_turn(caller.encode('utf-8'))
        resp = result.response_text
        print(f'\n  Turn {i+1}:')
        print(f'    CALLER: {caller}')
        print(f'    LUNA:   {resp[:200]}')
        if contains_medical_advice(resp):
            defects.append(f'Turn {i+1}: MEDICAL ADVICE in response')
    await rt.close()
    print(f'\n  Booking state: {rt.booking_collection.render()[:150]}')
    if defects:
        print(f'  DEFECTS: {defects}')
    else:
        print(f'  DEFECTS: none')
    print()
    return defects

async def main():
    all_defects = {}

    # 1. Happy path — straightforward Tamil booking
    d = await run_scenario("1. Happy path Tamil booking", [
        "Scaling appointment வேணும்",
        "நாளைக்கு",
        "மாலை 6:30",
        "கார்த்திக்",
        "ஆமா",
    ])
    all_defects["1_happy_path"] = d

    # 2. Karthick's original bug — date+time first, then reason
    d = await run_scenario("2. Date+time first (Karthick's bug scenario)", [
        "இன்னைக்கு எனக்கு 12 மணிக்கு appointment புக் பண்ணனும்.",
        "5 மணிக்கு ஓகே",
        "பல்லு வலிக்காக",
        "Karthick",
        "ஆமா",
    ])
    all_defects["2_date_first"] = d

    # 3. Self-correction mid-flow
    d = await run_scenario("3. Self-correction — change time mid-flow", [
        "Scaling appointment வேணும், நாளைக்கு",
        "10 am",
        "sorry, 10 வேண்டாம், 6:30 வேணும்",
        "Meena",
        "ஆமா",
    ])
    all_defects["3_self_correction"] = d

    # 4. Medical question during booking
    d = await run_scenario("4. Medical question mid-booking", [
        "Appointment வேணும், scaling",
        "ஒரு doubt — என்ன medicine எடுக்கணும் pain-க்கு?",
        "நாளைக்கு",
        "6:30",
        "Raja",
    ])
    all_defects["4_medical_question"] = d

    # 5. Sunday booking attempt
    d = await run_scenario("5. Sunday booking — should refuse", [
        "Sunday-ல appointment வேணும்",
    ])
    all_defects["5_sunday"] = d

    # 6. Multiple services asked
    d = await run_scenario("6. Multiple services confusion", [
        "Scaling-um root canal-um வேணும்",
        "scaling மட்டும் போதும்",
        "நாளைக்கு",
        "காலை 11",
        "Priya",
    ])
    all_defects["6_multi_service"] = d

    # 7. Price inquiry tangent
    d = await run_scenario("7. Price tangent mid-booking", [
        "Appointment வேணும்",
        "Scaling",
        "fee எவ்வளவு?",
        "ok, நாளைக்கு",
        "6:30 pm",
        "Karthick",
    ])
    all_defects["7_price_tangent"] = d

    # 8. Tanglish heavy code-mixing
    d = await run_scenario("8. Heavy Tanglish code-mixing", [
        "Bro, scaling appointment fix pannanum da",
        "Tomorrow evening",
        "6:30 works pa",
        "Karthick da",
        "Yes correct",
    ])
    all_defects["8_tanglish_heavy"] = d

    # 9. Elderly caller — formal Tamil
    d = await run_scenario("9. Formal Tamil — elderly caller", [
        "டாக்டர் அப்பாயிண்ட்மெண்ட் வேண்டும்",
        "பல்லு சுத்தம் செய்ய வேண்டும்",
        "நாளை வர முடியுமா?",
        "மாலை ஆறரை மணிக்கு",
        "சுரேஷ்",
    ])
    all_defects["9_formal_tamil"] = d

    # 10. Caller who gives everything in one shot
    d = await run_scenario("10. All info in one utterance", [
        "நாளைக்கு மாலை 6:30 scaling appointment வேணும், என் பேரு Karthick",
        "ஆமா",
    ])
    all_defects["10_all_in_one"] = d

    # 11. Wrong date correction
    d = await run_scenario("11. Date correction — today to tomorrow", [
        "இன்னைக்கு scaling appointment",
        "5 pm",
        "wait, இன்னைக்கு வேண்டாம், நாளைக்கு மாத்துங்க",
        "6:30",
        "Karthick",
    ])
    all_defects["11_date_correction"] = d

    # 12. Ambiguous time
    d = await run_scenario("12. Ambiguous time — bare 5", [
        "நாளைக்கு appointment வேணும், scaling",
        "5 மணி",
    ])
    all_defects["12_ambiguous_time"] = d

    # Summary
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    total_defects = sum(len(d) for d in all_defects.values())
    for name, d in all_defects.items():
        status = "FAIL" if d else "PASS"
        detail = f" — {', '.join(d)}" if d else ""
        print(f"  {status}: {name}{detail}")
    print(f"\nTotal: {len(all_defects)} scenarios, {total_defects} defects")

asyncio.run(main())
