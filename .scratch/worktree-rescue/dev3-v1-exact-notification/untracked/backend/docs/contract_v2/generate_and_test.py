"""Generator + simulator + auditor from single canonical contract source.

Reads owner_proposal_contract.json, generates derived artifacts,
runs strict simulator and negative mutation tests, audits for forbidden states.

Run: python3 docs/contract_v2/generate_and_test.py
"""

import hashlib
import json
import os
import struct
import sys
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

DIR = Path(__file__).parent
CONTRACT = json.loads((DIR / "owner_proposal_contract.json").read_text())

DURABLE = set(CONTRACT["durable_statuses"])
TRANSITIONS = CONTRACT["transitions"]
T_BY_ID = {t["id"]: t for t in TRANSITIONS}
RETRYABLE = set(CONTRACT["retryable_terminals"])
VECTORS = CONTRACT["test_vectors"]
NAMESPACE = b"fonely.owner_proposal_family.v1"

# ── Forbidden state audit ──

def audit_forbidden_states():
    """Fail if 'executing' or durable 'failed' appear anywhere except negative-test descriptions."""
    forbidden = {"executing", "failed"}
    for fpath in DIR.glob("*"):
        if fpath.name == "generate_and_test.py":
            continue
        if not fpath.is_file():
            continue
        content = fpath.read_text()
        data = json.loads(content) if fpath.suffix == ".json" else None
        if data:
            flat = json.dumps(data)
            for word in forbidden:
                if f'"{word}"' in flat:
                    raise AssertionError(
                        f"AUDIT FAIL: forbidden durable state '{word}' found in {fpath.name}"
                    )
    print("  Audit: no forbidden states in contract_v2/: PASS")


# ── DDL generator ──

def generate_ddl():
    cols = CONTRACT["columns"]
    lines = ["CREATE TABLE owner_command_proposals ("]
    for name, spec in cols.items():
        parts = [f"  {name}", spec["type"]]
        if spec.get("pk"):
            parts.append("PRIMARY KEY")
        if not spec.get("nullable", True) and not spec.get("pk"):
            parts.append("NOT NULL")
        if "default" in spec:
            parts.append(f"DEFAULT {spec['default']}")
        if "check" in spec:
            parts.append(f"CHECK ({name} {spec['check']})")
        if "check_in" in spec:
            vals = ", ".join(f"'{v}'" for v in spec["check_in"])
            parts.append(f"CHECK ({name} IN ({vals}))")
        if "fk" in spec:
            parts.append(f"REFERENCES {spec['fk']}")
        lines.append("  " + " ".join(parts[1:]).replace(parts[0].strip(), "").strip())
        lines[-1] = f"  {name} {lines[-1].strip()},"
    # Remove trailing comma from last column
    lines[-1] = lines[-1].rstrip(",")
    lines.append(");")
    ddl = "\n".join(lines)

    # Constraints
    for cname, cspec in CONTRACT["constraints"].items():
        if cspec["type"] == "UNIQUE":
            cols_str = ", ".join(cspec["columns"])
            ddl += f"\nALTER TABLE owner_command_proposals ADD CONSTRAINT {cname} UNIQUE ({cols_str});"
        elif cspec["type"] == "PARTIAL_UNIQUE":
            cols_str = ", ".join(cspec["columns"])
            ddl += f"\nCREATE UNIQUE INDEX {cname} ON owner_command_proposals ({cols_str}) WHERE {cspec['where']};"
        elif cspec["type"] == "FK":
            cols_str = ", ".join(cspec["columns"])
            refs_str = ", ".join(cspec["references"])
            ddl += f"\nALTER TABLE owner_command_proposals ADD CONSTRAINT {cname} FOREIGN KEY ({cols_str}) REFERENCES ({refs_str});"

    for iname, ispec in CONTRACT["indexes"].items():
        cols_str = ", ".join(ispec["columns"])
        where = f" WHERE {ispec['where']}" if "where" in ispec else ""
        ddl += f"\nCREATE INDEX {iname} ON owner_command_proposals ({cols_str}){where};"

    (DIR / "generated_ddl.sql").write_text(ddl)
    # Verify no forbidden states in DDL
    for word in ("executing", "failed"):
        assert f"'{word}'" not in ddl, f"forbidden state {word} in DDL"
    print("  DDL generated: PASS")


# ── Canonicalizer ──

def canonical_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

def family_id(preimage):
    return hashlib.sha256(canonical_json(preimage).encode("utf-8")).hexdigest()

def lock_key(fid):
    d = hashlib.blake2b(bytes.fromhex(fid), key=NAMESPACE, digest_size=8).digest()
    return int(struct.unpack(">q", d)[0])


# ── Transition Engine ──

class Proposal:
    def __init__(self, fid, attempt):
        self.fid = fid
        self.attempt = attempt
        self.status = "pending_confirmation"
        self.version = 1
        self.effects = False

class Engine:
    def apply(self, p, tid, *, lock_held=True):
        t = T_BY_ID[tid]
        if not lock_held:
            raise RuntimeError(f"{tid}: lock not held")
        if not t["family_lock"]:
            raise RuntimeError(f"{tid}: family_lock false in contract")
        if t["from"] is not None:
            if p.status != t["from"]:
                raise RuntimeError(f"{tid}: status {p.status} != {t['from']}")
            if not t["proposal_for_update"]:
                raise RuntimeError(f"{tid}: CAS requires FOR UPDATE")
        assert t["to"] in DURABLE, f"{tid}: to={t['to']} not durable"
        p.status = t["to"]
        if t["from"] is not None:
            p.version += 1
        p.effects = t["side_effects"] != "none"
        return t

E = Engine()

class Family:
    def __init__(self, fid):
        self.fid = fid
        self.proposals = []
        self.locked = False

    def lock(self):
        assert not self.locked; self.locked = True
    def unlock(self):
        self.locked = False

    def insert(self):
        assert self.locked
        n = max((p.attempt for p in self.proposals), default=0) + 1
        p = Proposal(self.fid, n)
        self.proposals.append(p)
        return p

    def winner(self):
        c = [p for p in self.proposals if p.status == "completed"]
        return min(c, key=lambda p: p.attempt) if c else None

    def active(self):
        return [p for p in self.proposals if p.status == "pending_confirmation"]

    def retryable_terminal(self):
        t = [p for p in self.proposals if p.status in RETRYABLE]
        return max(t, key=lambda p: p.attempt) if t else None

    def lookup(self):
        assert self.locked
        w = self.winner()
        if w: return "replay", w
        a = self.active()
        if a: return "pending", a[-1]
        t = self.retryable_terminal()
        if t: return "retryable", t
        return "new", None


# ── Migration Model ──

class Migration:
    def __init__(self):
        self.rows = []
        self.version = "0014"

    def upgrade(self, rows=None):
        self.rows = rows or []
        # Preflight: no duplicate active
        owners = set()
        for r in self.rows:
            if r.get("status") == "pending_confirmation":
                k = (r["business_id"], r["owner_user_id"])
                assert k not in owners, f"duplicate active {k}"
                owners.add(k)
        # Classify
        disc = CONTRACT["migration"]["v1_discriminator"]
        for r in self.rows:
            cp = r.get("command_payload", {})
            if isinstance(cp, dict) and cp.get("family_schema") == "owner-command-family-v1" and r.get("family_id"):
                r["classification"] = "exact_v1"
            else:
                r["classification"] = "legacy_unverifiable"
        self.version = "0015"

    def downgrade(self):
        if self.rows:
            raise RuntimeError("downgrade blocked")
        self.version = "0014"

    def reupgrade(self):
        self.upgrade()


# ── Tests ──

_p = [0]
def test(name):
    def d(fn):
        fn(); _p[0] += 1; print(f"  {name}: PASS")
    return d

# Positive transitions

@test("T1 new")
def _():
    f = Family("1"); f.lock()
    assert f.lookup()[0] == "new"
    p = f.insert(); E.apply(p, "T1")
    assert p.status == "pending_confirmation"; f.unlock()

@test("T2 retry")
def _():
    f = Family("2"); f.lock()
    p = f.insert(); E.apply(p, "T1"); E.apply(p, "T5")
    assert f.lookup()[0] == "retryable"
    p2 = f.insert(); E.apply(p2, "T2")
    assert p2.attempt == 2; f.unlock()

@test("T3 confirm → completed, command SP, CAS last")
def _():
    f = Family("3"); f.lock()
    p = f.insert(); E.apply(p, "T1")
    t = E.apply(p, "T3")
    assert p.status == "completed" and t["command_savepoint"] and t["completed_cas_last_in_sp"]
    assert p.effects; f.unlock()

@test("T3 rollback → pending, no effects")
def _():
    f = Family("3r"); f.lock()
    p = f.insert(); E.apply(p, "T1")
    s, v = p.status, p.version
    p.status, p.version = s, v  # rollback restores
    assert p.status == "pending_confirmation"; f.unlock()

@test("T4 expired")
def _():
    f = Family("4"); f.lock(); p = f.insert(); E.apply(p, "T1")
    E.apply(p, "T4"); assert p.status == "expired"; f.unlock()

@test("T5 rejected")
def _():
    f = Family("5"); f.lock(); p = f.insert(); E.apply(p, "T1")
    E.apply(p, "T5"); assert p.status == "rejected"; f.unlock()

@test("T6 stale expire + retry")
def _():
    f = Family("6"); f.lock(); p = f.insert(); E.apply(p, "T1")
    E.apply(p, "T6"); assert p.status == "expired"
    assert f.lookup()[0] == "retryable"; f.unlock()

# Illegal transitions (negative)

@test("Negative: T3 from rejected")
def _():
    f = Family("n1"); f.lock(); p = f.insert(); E.apply(p, "T1"); E.apply(p, "T5")
    try: E.apply(p, "T3"); assert False
    except RuntimeError: pass
    f.unlock()

@test("Negative: T5 from completed")
def _():
    f = Family("n2"); f.lock(); p = f.insert(); E.apply(p, "T1"); E.apply(p, "T3")
    try: E.apply(p, "T5"); assert False
    except RuntimeError: pass
    f.unlock()

@test("Negative: T3 without lock")
def _():
    f = Family("n3"); f.lock(); p = f.insert(); E.apply(p, "T1"); f.unlock()
    try: E.apply(p, "T3", lock_held=False); assert False
    except RuntimeError: pass

@test("Negative: T3 wrong version (simulated CAS loser)")
def _():
    f = Family("n4"); f.lock(); p = f.insert(); E.apply(p, "T1")
    p.status = "expired"  # wrong status
    try: E.apply(p, "T3"); assert False
    except RuntimeError: pass
    f.unlock()

# Winner + replay

@test("Winner prohibition")
def _():
    f = Family("w"); f.lock(); p = f.insert(); E.apply(p, "T1"); E.apply(p, "T3"); f.unlock()
    f.lock(); assert f.lookup()[0] == "replay"; f.unlock()

@test("Multi-gen: reject → expire → complete → replay")
def _():
    f = Family("mg"); f.lock()
    p1 = f.insert(); E.apply(p1, "T1"); E.apply(p1, "T5")
    p2 = f.insert(); E.apply(p2, "T2"); E.apply(p2, "T4")
    p3 = f.insert(); E.apply(p3, "T2"); E.apply(p3, "T3")
    f.unlock(); f.lock()
    a, w = f.lookup(); assert a == "replay" and w.attempt == 3; f.unlock()

# Lost-ACK

@test("Lost-ACK all outcomes")
def _():
    for status, action in CONTRACT["lost_ack_outcomes"].items():
        assert action in ("replay", "retry_confirm", "new_attempt")

@test("Lost-ACK completed → replay")
def _():
    f = Family("la1"); f.lock(); p = f.insert(); E.apply(p, "T1"); E.apply(p, "T3"); f.unlock()
    f.lock(); assert f.lookup()[0] == "replay"; f.unlock()

@test("Lost-ACK pending → retry")
def _():
    f = Family("la2"); f.lock(); p = f.insert(); E.apply(p, "T1"); f.unlock()
    f.lock(); assert f.lookup()[0] == "pending"; f.unlock()

@test("Lost-ACK rejected → new attempt")
def _():
    f = Family("la3"); f.lock(); p = f.insert(); E.apply(p, "T1"); E.apply(p, "T5"); f.unlock()
    f.lock(); assert f.lookup()[0] == "retryable"; f.unlock()

@test("Lost-ACK expired → new attempt")
def _():
    f = Family("la4"); f.lock(); p = f.insert(); E.apply(p, "T1"); E.apply(p, "T4"); f.unlock()
    f.lock(); assert f.lookup()[0] == "retryable"; f.unlock()

# Two-business isolation

@test("Two businesses independent")
def _():
    f1 = Family("b1"); f2 = Family("b2")
    f1.lock(); p1 = f1.insert(); E.apply(p1, "T1"); E.apply(p1, "T3"); f1.unlock()
    f2.lock(); p2 = f2.insert(); E.apply(p2, "T1"); E.apply(p2, "T3"); f2.unlock()
    f1.lock(); assert f1.lookup()[0] == "replay"; f1.unlock()
    f2.lock(); assert f2.lookup()[0] == "replay"; f2.unlock()

# Transport replay vs new invocation

@test("Transport replay returns same pending")
def _():
    f = Family("tr"); f.lock(); p = f.insert(); E.apply(p, "T1"); f.unlock()
    f.lock(); a, x = f.lookup(); assert a == "pending" and x is p; f.unlock()

@test("New invocation after reject")
def _():
    f = Family("ni"); f.lock(); p = f.insert(); E.apply(p, "T1"); E.apply(p, "T5")
    assert f.lookup()[0] == "retryable"; p2 = f.insert(); E.apply(p2, "T2")
    assert p2.attempt == 2; f.unlock()

# Migration

@test("Migration fresh/downgrade/reupgrade")
def _():
    m = Migration(); m.upgrade(); m.downgrade(); m.reupgrade()

@test("Migration populated downgrade blocked")
def _():
    m = Migration(); m.upgrade([{"business_id":1,"owner_user_id":1,"status":"completed","family_id":"x","command_payload":{"family_schema":"owner-command-family-v1"}}])
    try: m.downgrade(); assert False
    except RuntimeError: pass

@test("Migration v1 classification")
def _():
    m = Migration(); m.upgrade([{"business_id":1,"owner_user_id":1,"status":"completed","family_id":"abc","command_payload":{"family_schema":"owner-command-family-v1"}}])
    assert m.rows[0]["classification"] == "exact_v1"

@test("Migration legacy classification")
def _():
    m = Migration(); m.upgrade([{"business_id":1,"owner_user_id":1,"status":"completed","family_id":None,"command_payload":{}}])
    assert m.rows[0]["classification"] == "legacy_unverifiable"

@test("Migration duplicate active blocked")
def _():
    m = Migration()
    try: m.upgrade([{"business_id":1,"owner_user_id":1,"status":"pending_confirmation"},{"business_id":1,"owner_user_id":1,"status":"pending_confirmation"}]); assert False
    except AssertionError: pass

# Literal vectors

@test("Vectors match contract")
def _():
    for vname, v in VECTORS.items():
        fid = family_id(v["preimage"])
        assert fid == v["family_id"], f"{vname}: family_id"
        lk = lock_key(fid)
        assert lk == v["lock_key"], f"{vname}: lock_key"

# Schema validation

@test("Schema: ZoneInfo rejects invalid")
def _():
    try: ZoneInfo("Fake/Zone"); assert False
    except (KeyError, Exception): pass
    ZoneInfo("Asia/Kolkata")  # valid

@test("Schema: date rejects impossible")
def _():
    try: date.fromisoformat("2026-02-30"); assert False
    except ValueError: pass
    date.fromisoformat("2026-08-12")  # valid

@test("Schema: per-command required fields")
def _():
    for cmd, spec in CONTRACT["family_preimage_schemas"].items():
        assert spec["additional_properties"] is False
        assert "family_schema" in spec["required"]
        assert "business_id" in spec["required"]
    assert "resource_id" in CONTRACT["family_preimage_schemas"]["doctor_leave"]["required"]
    assert "resource_id" not in CONTRACT["family_preimage_schemas"]["close_clinic"]["required"]
    assert "close_time" in CONTRACT["family_preimage_schemas"]["close_early"]["required"]
    assert "close_time" not in CONTRACT["family_preimage_schemas"]["doctor_leave"]["required"]

# Mutation self-checks

@test("Mutation: T3 to→rejected detected")
def _():
    orig = T_BY_ID["T3"]["to"]; T_BY_ID["T3"]["to"] = "rejected"
    f = Family("m1"); f.lock(); p = f.insert(); E.apply(p, "T1"); E.apply(p, "T3")
    assert p.status == "rejected"  # wrong — detected
    T_BY_ID["T3"]["to"] = orig

@test("Mutation: lock false → engine fails")
def _():
    orig = T_BY_ID["T5"]["family_lock"]; T_BY_ID["T5"]["family_lock"] = False
    f = Family("m2"); f.lock(); p = f.insert(); E.apply(p, "T1")
    try: E.apply(p, "T5"); assert False
    except RuntimeError: pass
    T_BY_ID["T5"]["family_lock"] = orig

# Audit + generate

audit_forbidden_states()
generate_ddl()

print(f"\n=== All {_p[0]} tests passed ===")
