from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from pathlib import Path

from voice_eval.metrics import word_error_counts

TOKEN_EQUIVALENTS = {
    "doctor":"doctor","dr":"doctor","டாக்டர்":"doctor","priya":"priya","பிரியா":"priya","பிரிய":"priya","arjun":"arjun","அர்ஜுன்":"arjun",
    "appointment":"appointment","அப்பாயின்ட்மென்ட்":"appointment","அப்பாயின்மென்ட்":"appointment","book":"book","புக்":"book",
    "scaling":"scaling","ஸ்கேலிங்":"scaling","ஸ்கேலிங்க்கு":"scaling","rate":"rate","ரேட்":"rate",
    "cold":"cold","கோல்டு":"cold","water":"water","வாட்டர்":"water","sensitive":"sensitive","சென்சிட்டிவா":"sensitive",
    "evening":"evening","ஈவினிங்":"evening","available":"available","அவைலபிலா":"available","slot":"slot","ஸ்லாட்":"slot",
    "office":"office","ஆபீஸ்":"office","root":"root","ரூட்":"root","canal":"canal","கெனால்":"canal","கனால்":"canal",
    "six":"6","6":"6","06":"6","ஆறு":"6","seven":"7","7":"7","ஏழு":"7","eleven":"11","11":"11","பதினொன்று":"11","thirty":"30","30":"30","முப்பது":"30",
    "naalaikku":"நாளைக்கு","நாளைக்கு":"நாளைக்கு","naala":"நாளா","நாளா":"நாளா","rendu":"ரெண்டு","ரெண்டு":"ரெண்டு","2":"ரெண்டு",
    "pallu":"பல்லு","பல்லு":"பல்லு","tooth":"பல்லு","டூத்":"பல்லு","vali":"வலி","வலி":"வலி","pain":"வலி",
    "cancel":"cancel","கேன்சல்":"cancel","illa":"இல்ல","இல்ல":"இல்ல","இல்லை":"இல்ல","vendaam":"வேண்டாம்","வேண்டாம்":"வேண்டாம்","வேணாம்":"வேண்டாம்",
    "venum":"வேணும்","வேணும்":"வேணும்","pannanum":"பண்ணணும்","pannanu":"பண்ணணும்","பண்ணணும்":"பண்ணணும்","பண்ணனும்":"பண்ணணும்",
    "paakanum":"பாக்கணும்","பாக்கணும்":"பாக்கணும்","பார்க்கணும்":"பாக்கணும்","direct":"direct","டைரக்டா":"direct",
    "mani":"மணி","மணி":"மணி","saturday":"saturday","சாட்டர்டே":"saturday","morning":"morning","மார்னிங்":"morning",
    "okay":"okay","ஓகே":"okay","earliest":"earliest","ஏர்லியஸ்ட்":"earliest","enakkum":"எனக்கும்","எனக்கும்":"எனக்கும்",
    "wife":"wife","வைஃப்":"wife","வைஃப்க்கும்":"wife","back":"back","பேக்":"back","to":"to","டு":"to"
}


def raw_tokens(text: str) -> list[str]:
    text = unicodedata.normalize("NFC", text).casefold()
    text = re.sub(r"(?<=[0-9a-z])(?=[஀-௿])|(?<=[஀-௿])(?=[0-9a-z])", " ", text)
    return re.sub(r"[^0-9a-z஀-௿₹]+", " ", text).split()


def semantic_tokens(text: str) -> list[str]:
    return [TOKEN_EQUIVALENTS.get(token, token) for token in raw_tokens(text)]


def semantic_text(text: str) -> str:
    return " ".join(semantic_tokens(text))


def entity_present(entity: dict, hypothesis: str) -> bool:
    hyp = semantic_tokens(hypothesis)
    for value in [entity["value"], *entity.get("variants", [])]:
        candidate = semantic_tokens(value)
        if candidate and any(hyp[i:i+len(candidate)] == candidate for i in range(len(hyp)-len(candidate)+1)):
            return True
    return False


def classify_row(fixture: dict, result: dict) -> dict:
    ref=fixture["reference"]["transcript"]; hyp=result["output"]["raw_transcript"]
    semantic=word_error_counts(semantic_text(ref),semantic_text(hyp),fixture["locale"])
    entities=[e for e in fixture["reference"]["critical_entities"] if e.get("critical",True)]
    semantic_correct=sum(entity_present(e,hyp) for e in entities)
    if result["status"]!="passed": category="provider_failure"
    elif result["metrics"]["raw_exact_match"]: category="exact"
    elif semantic.wer==0 and semantic_correct==len(entities): category="script_form_only"
    elif semantic_correct<len(entities): category="critical_entity_error"
    else: category="semantic_recognition_error"
    return {"fixture_id":result["fixture_id"],"prompt_id":fixture.get("prompt",{}).get("prompt_id"),"mode":result["provider"]["mode"],"status":result["status"],"category":category,"reference":ref,"raw_transcript":hyp,"raw_wer":result["metrics"]["wer"],"semantic_wer":semantic.wer,"critical_entity_correct_raw":result["metrics"]["critical_entity_correct"],"critical_entity_correct_semantic":semantic_correct,"critical_entity_total":len(entities),"provider_confidence":result["output"]["provider_confidence"],"provider_latency_ms":result["timing"]["wall_ms"],"errors":result["errors"],"review_status":"pending" if category not in {"exact","script_form_only"} else "auto_categorized"}


def build_analysis(fixtures: list[dict], results: list[dict], retries: list[dict] | None = None) -> dict:
    fmap={f["fixture_id"]:f for f in fixtures}; rows=[classify_row(fmap[r["fixture_id"]],r) for r in results]
    retry_map={(r["fixture_id"],r["provider"]["mode"]):r for r in (retries or [])}
    for row in rows:
        retry=retry_map.get((row["fixture_id"],row["mode"]))
        if retry:
            row["retry"]={"run_id":retry["run_id"],"status":retry["status"],"raw_transcript":retry["output"]["raw_transcript"],"provider_confidence":retry["output"]["provider_confidence"],"provider_latency_ms":retry["timing"]["wall_ms"]}
    categories=Counter(r["category"] for r in rows); by_mode={}
    for mode in sorted({r["mode"] for r in rows}):
        values=[r for r in rows if r["mode"]==mode]; passed=[r for r in values if r["status"]=="passed"]; total=sum(r["critical_entity_total"] for r in passed)
        by_mode[mode]={"attempted":len(values),"passed":len(passed),"raw_macro_wer":sum(r["raw_wer"] for r in passed)/len(passed) if passed else None,"semantic_macro_wer":sum(r["semantic_wer"] for r in passed)/len(passed) if passed else None,"raw_critical_exactness":sum(r["critical_entity_correct_raw"] for r in passed)/total if total else None,"semantic_critical_exactness":sum(r["critical_entity_correct_semantic"] for r in passed)/total if total else None}
    queue=sorted([r for r in rows if r["review_status"]=="pending"],key=lambda r:(r["category"]=="provider_failure",r["critical_entity_total"]-r["critical_entity_correct_semantic"],r["semantic_wer"]),reverse=True)
    return {"analysis_version":1,"rows":rows,"summary":{"categories":dict(categories),"by_mode":by_mode,"review_queue_count":len(queue)},"review_queue":queue}


def write_analysis(path: Path, analysis: dict):
    path.parent.mkdir(parents=True,exist_ok=True); path.write_text(json.dumps(analysis,ensure_ascii=False,indent=2,sort_keys=True)+"\n")
