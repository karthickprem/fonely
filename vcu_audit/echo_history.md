# ECHO — Historical Defect Intelligence: Everest/Telluride VCU2

**Analyst:** defect-echo (ECHO)
**Date:** 2026-08-12
**Status:** IN PROGRESS — history discovery underway; target tuple NOT frozen.

> **RETRACTION LOG**
> - **2026-08-12 (INV-RESET-ISOLATION-01):** My earlier interpretation that EDT-1069519 closed as a **SW-only reset workaround with no HW reset added** is **RETRACTED**. It was based on interim Jira comments alone. Later-supplied DID design description + supplied RTL port presence show two independent HW reset pins were defined (`pl_vcu2_spare_in[56]` ENC, `[11]` DEC). See §6. Functional connectivity still unverified.
>
> **SUPPLIED-SOURCE DISCLAIMER:** The DID (`Telluride_DID_1.5_VCU.docx`) and the RTL tree (`vcu2_v1_7t`) are **supplied candidate sources**, not confirmed authorities for the user's audit target. Document authority/precedence (Arch Spec vs DID) and **target applicability of this RTL snapshot are UNRESOLVED**. Port-level RTL presence = implementation evidence only, never functional-fix proof.

---

## 0. Scope & provenance guardrails (per SENT-004 / Orion corrections)

- **Audit target = EVIDENCE_PENDING.** The tuple `Telluride T50-A0 / VCU2 / vcu2_v1_7t` is an **inferred identity / search hypothesis only**, derived from local paths + Confluence family data. It is **NOT** a user-frozen scope. Target SKU/stepping, release, enabled codecs/features, and RTL-snapshot applicability remain **BLOCKED** (SENT-001 open).
- **Authority for the "T50-A0" family string:** Confluence page **793871409** (space XSIC, "Everest"), row *"Telluride, T50-A0 Production - Taped Out"* — establishes a **family variant**, not the audit target.
- **RAG = discovery/leads only.** Every retained claim below is verified against an original Jira record or the on-disk DID/Arch authority.
- **Search breadth:** aliases `Everest MMD VCU` / `Telluride VCU` / `VCU2` / `MMD-VCU`, block tokens `vcu2_v1_6t0`, `vcu2_v1_6t`, `vcu2_v1_7t`, across steppings `T10/T20/T40/T50`.

---

## 1. Source status (this session)

| Source | Access | Status | Notes |
|---|---|---|---|
| Xilinx Jira (`sage.search_xilinx_jira`) | authenticated | **LIVE but HTTP 429 rate-limited** | Errata authority = **EDT** project; also PVS (validation), CR. Serial calls + backoff required; 3-parallel trips the limit. |
| cloud_atlassian **Jira** | 401 Unauthorized | **UNUSABLE for Jira** | Token is Confluence-only. |
| cloud_atlassian **Confluence** | authenticated | **LIVE** | Spaces: XPS (VCU2 verif/test/PD/archive), XSIC (Everest hub, Silicon Errata). |
| Docs RAG (`sage.search_docs`) | LIVE | leads only | T50 VnC bring-up plans, MMD-VCU test plans. |
| GitHub (`karthick_amdeng`) | authenticated | **0 repos visible** | AMD VCU2 RTL is **Perforce**, not git → GitHub likely NOT the history authority. Flagged. |
| XRDB | **DOWN** | "Unable to connect" | Register cross-check unavailable. |
| Local RTL | readable | **Perforce** | `.icmconfig`: P4CLIENT=`yadav+everest+v1+35`, P4PORT=`xhdicmsuper:1777`. `p4` not in PATH → exact changelist (= "target commit") is a **BLOCKER**. **Primary supplied candidate source, not confirmed target.** |
| Local DID/Arch | readable | **primary supplied candidate source** (NOT confirmed release authority) | `Telluride_DID_1.5_VCU.docx`, `Everest_MMD_VCU_Arch_Specs_1.2 (1).pdf`. Release authority/precedence unresolved (**D7 open**). |

**Authoritative Jira scopes discovered** (from Everest hub 793871409): `jira.xilinx.com` filter=46122 (all EDT Silicon Issues); Everest Open Silicon Issues Dashboard pageId=38111; per-device EDT Trackers (T10 pageId=63623, L40=58321).

---

## 2. Supplied RTL snapshot (local, Perforce) — inferred family mapping, NOT confirmed target

- Product (INFERRED FAMILY MAPPING, not confirmed target): **Everest / Telluride VCU2**; block dir `rtl/vcu2_v1_7t` in the supplied 12-March VERIF drop. Target SKU/stepping/release remain BLOCKED (SENT-001).
- Block submodules present: `vcu2_core`, `vcu2_core_top`, `vcu2_dec_top`, `vcu2_enc_top`, `vcu2_enc_core_top`, `vcu2_interconnect`, `vcu2_reset`, `vcu2_slcr_regs`, `vcu2_npi_regs`, `vcu2_apm`, `vcu2_atom`, `vcu2_bpd_por`, `vcu2_ssc_rx`, iso/noc wraps, `vcu2_vivado_*_wrap` (enc/dec/LLM/top).
- **Lineage evidence (not yet a regression surface):** Confluence archive (607694312) references prior block `vcu2_v1_6t0_block`; current RTL is `vcu2_v1_7t`. 6t0→7t is a **naming/version step**; will only be called a regression surface once a concrete change/fix is mapped across the two.

---

## 3. Arch Spec release lineage (Orion target #1) — EVIDENCE_PENDING: DID v1.0 pin found, release authority UNRESOLVED (D7 open)

**Source (primary supplied candidate):** on-disk `Telluride_DID_1.5_VCU.docx`, §4.2 Functional Requirements + Revision History. *This DID is a supplied candidate document, not a confirmed release authority.*

- DID §4.2 verbatim: *"Reference: **Everest MMD VCU Arch Specs v 1.0** ... Dual core encoder and single core decoder..."*
- DID hyperlink text points to: `.../arch/Shared Documents/Arch Specs (released)/Telluride/MMD/**Everest_MMD_VCU_Arch_Specs_1.0.pdf**`.
- **Finding (association, not authority resolution):** The DID **references Arch Spec v1.0** and its link text sits under an "Arch Specs (released)" path — this proves a *reference*, NOT that v1.0 is the governing release authority for the audit target. The copy staged locally is **v1.2** (`Everest_MMD_VCU_Arch_Specs_1.2 (1).pdf`); the DID does **not** reference v1.2. **Which version is the approved/current authority (v1.0 vs v1.2), and its target applicability, is UNRESOLVED (architecture-authority issue D7 remains OPEN).** Must be confirmed against the SharePoint "Arch Specs (released)" folder original. Version gap v1.0(referenced) → v1.2(on disk) is a documentation-authority delta to resolve, not a defect.

---

## 4. DID Revision History — direct EDT fix mapping (verbatim)

**Source (primary supplied candidate):** `Telluride_DID_1.5_VCU.docx` Revision History table. Records editor change-notes; not a confirmed release authority.

| Ver | Date | Editor | Comment (verbatim) |
|---|---|---|---|
| 1.2.0 | 08/20/2023 | Anurag Agrawal | Sysmon Implementation |
| 1.5.0 | 09/10/2023 | Anurag Agrawal | Interrupt Tree Diagram; DFX Arch Updates; NPI Interrupt – Scan clear & mem clear updates; Partial reconfiguration proposal; Area Estimation Updates |
| **1.5.1** | **07/15/2024** | **Abhay Galagali** | **"EDT-1069519 fix: Updated reset section. EDT-1072959 fix: Updated Abus Switch DPSTx2 section"** |
| 1.5.1 | 31/10/2025 | Abhay Galagali | Cleaned-up TBDs |

**→ EDT-1072959 mapping = CONTR-001 CLOSED `INVESTIGATED_NO_ISSUE` (source-cell corruption/misassociation only), per Sentinel disposition 2026-08-12:** The Jira *title* of EDT-1072959 is *"Reify Assertions Failures for the Power Aware Scenarios"*; the DID change-note associates its *fix* with *"Updated Abus Switch DPSTx2 section."* Resolving evidence: Ledger's raw-XML audit + a second DID hyperlink + Echo's EDT-1076244 ABUS-assertion evidence (the waiver concerns the same `ams_abus_switch_DPSTx2` reify assertions) together show **EDT-1072959 describes the Reify symptom and the DID records the ABUS DPSTx2 section being updated for that same issue** — the apparent ID/section "mismatch" was a source-cell/association artifact, NOT a real contradiction. **Scope of this closure is NARROW: it resolves only the source-cell corruption/misassociation.** It does **NOT** prove implementation correctness, waiver validity, or target applicability (those remain EVIDENCE_PENDING). Do not attach ABUS/DPSTx2 to any *other* ID.

---

## 5. DID Open Issues table (verbatim) — Orion target #4: DID-recorded closures (target applicability UNVERIFIED)

**Source (primary supplied candidate):** `Telluride_DID_1.5_VCU.docx` "Open Issues" body table. Records intended/decided resolutions; not proof of target-RTL behavior.

| # | Status | Title | Resolution (verbatim, condensed) |
|---|---|---|---|
| 1 | Open→**Closed** (per DID) | **Multi core decoder** | DID-recorded text: Arch specs define decoder has only one core. "Multi-core is a general term. But the later explanation in architecture specs [says] decoder is single core based." (Attributed to Ygal, email — an email summary recorded in the DID, not independently verified.) |
| 2 | Open→**Closed** (per DID) | **Support I, IP, and IPB decoding** | DID-recorded text: "In the architecture specs, nothing is mentioned about IPB decoding." Attributed to Ygal: "Decoder must decode any stream, while encoder can pick and choose features. Decoder must support I, P and IPB decoding." (Email summary recorded in DID; not independently verified.) |
| 3 | Closed | APM Interface on AXI MCU Interface | Keep APMs on **all** AXI interfaces (not resource intensive). |
| 4 | Closed | VCU SLCR & Secure SLCR Registers | AXI PROT does not come out from Enc/Dec IP; VDU has AXI Prot in Non-secure SLCR. |
| 5 | Closed | NoC – PL Feedthrough | Bypass signals NoC→PL; no buffering in VCU domain. |
| 6 | Open→Closed | BPD POR Instance | BPD POR with 4 power supplies, two instances. |

**→ Orion target #4 (decoder single-core + I/P/IPB "external/email resolutions"):** Both resolutions are **recorded in the DID Open Issues table** as email summaries attributed to **Ygal**. As DID-recorded resolutions: decoder = single core; decoder must support I, P, IPB decoding. **These are DID-recorded (candidate-source) statements — NOT confirmed; the underlying email originals are not in hand, and RTL presence/behavior in `vcu2_dec_top` on the supplied snapshot is UNVERIFIED (EVIDENCE_PENDING).** Must be checked before any disposition.

---

## 6. EDT issues opened (original Jira records)

### EDT-1069519 — "Independent Reset for Encoder and Decoder" (Orion target #2) — CORRECTED after multi-authority reconciliation
- **Status:** Closed. **Assignee:** Galagali, Abhay. **Labels:** FUSA_NO_IMPACT, rtl_risk. **Created** 2024-03-05, **Updated** 2024-07-15.
- **Root cause (verbatim):** Arch spec requires *independent hardware reset* for Encoder and Decoder; *"the present implementation is single hardware reset for both."*

**THREE sources, THREE lifecycle stages — reconciled (this corrects my earlier Jira-only read). Authority/precedence and target applicability remain UNRESOLVED:**
1. **Jira comments (INTERIM, Mar–Jul 2024):** *"independent reset is possible using **software resets** ... current soft resets available shall work similarly like VCU1."* Verif: *"checked in waves and the associated testcases are passing."* → This was an **interim verification-side disposition / SW proposal**, NOT the final closure rationale.
2. **Later-supplied DID design description (v1.5.1, 07/15/2024, "Updated reset section"):** describes **two independent HW reset input pins** — `pl_vcu2_spare_in[56]` **(ENC RAW RST)** = *"Encoder core reset input from PL. Serves as reset to Encoder core logic in both L1 and L2"*; `pl_vcu2_spare_in[11]` **(DEC RAW RST)** = *"Decoder core reset input from PL. Serves as reset to Decoder core logic."* (Shared `pl_vcu2_du_raw_rst_n` still resets SLCR/Interconnect/DEC/ENC/APMs.) *Note: this DID is a supplied design description; its formal-release status/precedence vs Arch Spec is unresolved.*
3. **Supplied RTL snapshot `vcu2_v1_7t` (implementation evidence — port presence only):** both pins present as real ports/hookups in `vcu2_core_top/rtl/blh/SHIP_VCU2_VCU2_vcu2_core.v`:
   - L1051 `hookup_PLVCU2SPAREIN56_pl_vcu2_spare_in56 ( ... PLVCU2SPAREIN[56], _pl_vcu2_spare_in[56] )`
   - L1006 `hookup_PLVCU2SPAREIN11_pl_vcu2_spare_in11 ( ... PLVCU2SPAREIN[11], _pl_vcu2_spare_in[11] )`

- **CORRECTED verdict:** Atlas + Trace corroborated on the mechanism — EDT-1069519's DID design description **describes an intended HW-reset fix** (independent HW reset pins), the supplied RTL **shows port presence**, and Trace confirms the **compiled byte closure implements independent disjoint reset cones** (see Trace finding below). This establishes the **fix is structurally present in the supplied byte closure** — it does **NOT** prove target-silicon resolution. My earlier "no independent HW reset added" (interim-Jira-only read) is **RETRACTED**. **Port/structural presence = implementation evidence on the supplied closure, not silicon/target proof.**
- **Trace-reported structural finding (rtl-tracer, received 2026-08-12) — REPORTED, pending Sentinel audit:** Trace *reports* the supplied byte closure implements independent, disjoint HW reset cones for encoder and decoder, and *reports* the BLH `atom_if_wrap` tie-off branch (`PLVCU2SPAREIN[11]` default "0") as non-compiled collateral. **These are Trace-reported claims, NOT independently confirmed** — pending Sentinel's audit of Trace's closure/manifest and the ~375 missing refs, plus Breaker/Prover (both HELD). My earlier tie-off caveat is *provisionally* addressed for the reported-analyzed view only. Any "fix structurally present" reading is **Trace-reported, NOT target-silicon proof.**
- **Fields still to pull (429 backoff):** affectedVersions, fixVersions, issue links, CL#, duplicates.
- **Disposition (nonterminal): `EVIDENCE_PENDING`.** The narrow structural hypothesis — that independent disjoint enc/dec HW reset cones exist — is **reported rejected as a defect for the supplied *analyzed filelist* closure** (Trace: compiled closure implements independent reset cones; BLH tie-off = non-compiled collateral). This is **NOT yet an accepted "fix structurally present" conclusion**: pending (a) Sentinel evidence audit of Trace's closure/manifest and the ~375 missing refs, (b) Breaker/Prover review, (c) Jira affected/fixVersion + changelist/backport, (d) target/dynamic applicability. No terminal state.

### EDT-1072959 — "Reify Assertions Failures for the Power Aware Scenarios" (Orion target #3)
- **Status:** Closed. **Assignee:** Balasubramanyam Seetharaman, Prasanna. **Labels:** FUSA_NO_IMPACT, REIFY_T50, rtl_risk. **Created** 2024-04-30, **Updated** 2024-07-26.
- **Scope (verbatim):** Assertion failures on various power-supply combinations when running **reify models in Jasper**.
- **Design update (verbatim comment, Galagali):** *"integrated the uniquified OR gate released by the PoR IP team. RTL, UPF, PD Netlist (DFT and LAY) complete. Qualified with: Sanity (MBIST) regression, UPF-enabled test-case sim, RTL CLP, LEC (RTLvsDFT and DFTvsLAY)."*
- **Waiver linkage (verbatim):** *"We have filed for waiver approval ticket **EDT-1076244**. 2 Reify assertions are failing, they may be waivers. Awaiting response from johniea / AMS team."* Final: *"The new model delivered is clean, and we don't observe any assertion errors."*
- **ECHO note (CONTR-001 OPEN — no agreement asserted):** The DID change-note *associates* EDT-1072959 with the "Abus Switch DPSTx2 section" update, and the linked waiver EDT-1076244 (below) concerns the same `ams_abus_switch_DPSTx2` reify assertions — this **strongly explains** the association but does **NOT** establish that the two sources "agree" on a causal fix. Retain **EVIDENCE_PENDING** until (a) Ledger completes the DID raw/rendered source-cell integrity audit and (b) the exact EDT-1072959 ↔ EDT-1076244 issue-link relationship is recorded from Jira. Only then propose explicit contradiction resolution. Do not treat Jira resolution text as implementation proof.

### EDT-1076244 — "Waiver Approval for VCU2 Reify Assertions Part of the AMS switch" (linked waiver of EDT-1072959)
- **Status:** Closed. **Assignee:** Au, Johnie. **Created** 2024-07-23, **Updated** 2024-07-26. No labels.
- **Scope (verbatim):** with macro `REIFY_BUS_CHECKS` enabled, two reify assertions on the AMS abus switch fail:
  - `...i_vcu2_ams_abus_switch_DPSTx2.Reify_Xcheck__if_abus__n.Icheck.is_false_check`
  - `...i_vcu2_ams_abus_switch_DPSTx2.Reify_Xcheck__if_abus__p.Icheck.is_false_check`
  from `ams/rtl/ams_abus_switch_DPSTx2.sv`. Reporter: *"as per feedback from you these assertions are needed only at the full chip level."*
- **Diagnostic (verbatim comment):** *"observing the if_abus__n and if_abus__p to be driven with the value **z** irrespective of the power supplies."*
- **Resolution (verbatim final comment):** *"the issue was seen because of the **Jasper tool artifact**."*
- **ECHO note (reported Jira disposition, NOT established fact):** The Jira thread's final comment *asserts* the failure was a "Jasper tool artifact," and the reporter *states* the assertions were "needed only at full chip level" (basis for the waiver request). These are **historical author assertions / reported Jira disposition** — NOT independently proven. Whether it is truly a tool artifact vs a real design/X-propagation issue, and the exact waiver semantics/approval state, are **EVIDENCE_PENDING** pending independent target evidence and waiver-record check. The identifier `i_vcu2_ams_abus_switch_DPSTx2` appears as instance text in the supplied RTL; whether it is an **active/elaborated instance in the analyzed config is UNRESOLVED** (no elaboration/config proof yet). The reported `z`-driven `if_abus__n/__p` behavior is not independently confirmed. **Recorded as an EVIDENCE_PENDING historical lead — NOT handed to Breaker (Breaker is HELD by Orion/Sentinel).**

---

## 6c. Silicon-impact ranking of open leads (per Karthick/Orion: direct progress, top-2 opened)

Ranked by silicon impact × evidence accessibility:

| Rank | ID | Status | Silicon impact | Accessibility | Note |
|---|---|---|---|---|---|
| **1** | **EDT-1098410** | **Assigned (OPEN)** | HIGH — isolation/reset correctness (PL-MMD domain clamp) | HIGH — DID §5.3.3 in hand, registers named, XSDB steps given | **Live requirement DISPUTE** reporter(Karthick) vs designer(Galagali). Opened below. |
| **2** | **EDT-1093413** | **New (OPEN)** | LOW — spec-interpretation, likely not a HW defect | HIGH — Allegro confirmed semantics | Opened below; likely doc fix. |
| 3 | EDT-1094741 | Closed | MED — IR waiver in low-latency encoder fix db | MED | waiver — pull next |
| 4 | EDT-1096525 | Fixed | MED — T3 VCU2 isolation waiver | MED | isolation waiver — pairs w/ 1098410 |
| 5 | EDT-1095022 | Closed | LOW — 12bit pitch config doc | MED | buffer/format doc |

### EDT-1098410 — "VCU2: Reset/control registers retain state after asserting MMD_NPI_PCSR_CONTROL[24] (ctrl_iso_1_mmd_pl)" — RANK 1, OPEN
- **Status:** Assigned. **Assignee:** Galagali, Abhay. **Reporter:** S, Karthick. **Labels:** FoundBySival, FoundbyPVS, T40. Created 2026-07-20, Updated 2026-07-21.
- **Expected (reporter):** asserting isolation bit[24] `ctrl_iso_1_mmd_pl` (reg `0xF63E0004`) should make affected VCU2 regs (APM0_CFG `0xE8000110`, ENC_AXI_PROT_QOS `0xE8000078`, APM0_RESULT0/1, ISR_1) reflect **reset/default** state.
- **Observed (reporter, T40 ES1 / XSDB):** wrote APM0_CFG=0x6, set bit[24], re-read APM0_CFG=**0x6 UNCHANGED**. No register returned to reset. "Isolation had no observable effect on VCU2."
- **Requirement basis (reporter cites DID §5.3.3) — exact excerpt + table context (echo-verified verbatim):** located under **"Table 9: Power Domains Isolation and Clamping Values" / "Figure 12 VDU Power domain"**. Full surrounding passage: *"Ready Signals of AXI interfaces are clamped to 0. When the block is powered off, there will be no data communication. Valid Signals of AXI interfaces are **proposed** to clamp with bit '0' ... **Reset signals are proposed to clamp with asserted state values (reset is in asserted State when PL power down)** ... Unused signals of AXI interfaces are **proposed** to tie with constants."* A parallel finalized subsection "Isolation and Clamp Values" restates SIGNAL clamps: *"Reset signals like **pl_vdu_raw_rst_n**, pl_vdu_scan_mode_rst_n_ext are clamped to '0' (reset is in asserted State when PL power down)."*
  - **PROPOSAL vs REQUIREMENT (key):** the reporter's cited sentence uses "**proposed**" — draft intent, NOT a ratified MUST. It is **VDU-derived** boilerplate (names `pl_vdu_raw_rst_n`, VDU power domain) about **SIGNAL clamp values at a power-domain crossing on PL power-DOWN**, not register-content reset on a SW isolation-control-bit write. Distinguish sharply: this is a proposal about signal clamping, not a requirement that iso-bit[24] resets register CONTENTS.
- **DISPUTE (unresolved, verbatim):** Galagali: *"I do not understand the intent ... are you saying if this VCU2-PL s.w iso ctrl register bit is set then VCU2 should be reset and all internal registers be cleared?"* → *"No - not clear. Can you point to the spec which lists this requirement?"* Karthick points to DID §5.3.3.
- **ECHO note (interpretation gap):** DID §5.3.3 says reset *signals* clamp to *asserted state* on PL power-down — signal clamping at the isolation boundary, word "**proposed**" = non-finalized. Whether that mandates **register CONTENTS return to reset** (reporter) vs merely clamps the reset *signal* (designer) is the interpretation gap.
- **ATLAS ARCHITECTURE RULING (arch-atlas, received 2026-08-12) — FAVORS DESIGNER; reporter's requirement NOT found as an on-disk MUST:**
  - The cited DID passage (§"Isolation and Clamping Values", S2:2663 verbatim *"Reset signals are proposed to clamp with asserted state values (reset is in asserted State when PL power down)"*) governs **BOUNDARY SIGNAL clamping of reset signals during PL POWER-DOWN**, NOT register-content reset on a SW iso-control bit. "proposed" = non-finalized intent, not a ratified MUST.
  - **Semantic distinction:** `ctrl_iso_1_mmd_pl` is an ISOLATION control, NOT a reset. DID S2:914-915: *"MMD-PL Isolation control ctrl_iso_1_mmd_pl releases isolation between VCU2 and PL domains ... Iso_pl_vcu2_n is generated by ANDing hw_iso_pl_vcu2_n & ctrl_iso_1_mmd_pl."* It gates signals crossing the PL↔VCU boundary; it does not reset the VCU-domain register array.
  - The affected regs (APM0_CFG, APM0_RESULT0/1, ENC_AXI_PROT_QOS, ISR_1) live in VCCINT_VCU domain (DID power-domain table S2:2635-2637), NOT powered down by a PL-side iso bit. Register reset is governed by the RESET tree (INITSTATE/IPOR/por), NOT iso bits (DID NPI-reset table S2:1865-1878). **Registers RETAINING state after bit[24]=1 is CONSISTENT with documented behavior (isolation ≠ reset).**
  - Arch Spec v1.2 (S1): **NO isolation-register-reset requirement (0 hits).** Reporter's requirement lacks an on-disk architecture MUST.
  - **What would overturn:** an Arch Spec MUST (S1) or a finalized (non-"proposed") DID/register-spec statement that bit[24] triggers register-content reset. NONE found in S1/S2/S5.
  - **Naming reconciled (Atlas):** `ctrl_iso_1_mmd_pl` ≡ `ctrl_iso_1_vcu2_pl` ≡ `iso_1_vdu_pl` = MMD_NPI_PCSR_CONTROL[24]; vcu2/vdu variants are VDU-derived naming residue for the SAME bit. S5 RTL canonical = CTRL_ISO_1_MMD_PL (bit24).
- **ECHO disposition (per Sentinel audit wording): `EVIDENCE_PENDING; original register-reset hypothesis lacks governing requirement and is not candidate-eligible.`** The executed T40 ES1 register-retention observation is established; the alleged register-reset violation is **unsupported by the cited supplied text** — a "PROPOSED" signal clamp during PL power-down does not impose register clearing on SW isolation bit[24], and the design owner requests a missing spec. Not a verdict ("NOT-A-BUG" language avoided as premature). Remaining audit trail: Arch Spec v1.0/v1.1 check (v1.2 = 0 hits, Atlas); Jira record off-disk fields (Ledger) in case a later comment adds authority.
- **SEPARATE OPEN QUESTION (do NOT consider resolved by retention semantics): dynamic access / quiesce.** Legal writes/reads and in-flight AXI traffic **while isolation bit[24] is asserted** may have an INDEPENDENT contract and impact (e.g. transactions crossing the clamped boundary, hang/SLVERR, quiesce/drain requirements). This is distinct from the register-content-reset hypothesis and is NOT dispositioned. Route to Atlas (isolation-quiesce contract) / Trace / Breaker if opened. EVIDENCE_PENDING.

### EDT-1093413 — "[BURST][VCU2][Encoder] STAT_000 bytes differ core0 vs core1" — RANK 2, OPEN(New)
- **Status:** New. Unassigned. **Labels:** burst_found_documentation. Created 2025-08-14, Updated 2026-08-02.
- **Symptom:** multi-core, some resolutions: `STAT_000[29:0]` (bitstream size in bytes) differs core0 vs core1. E.g. 536x128: `0xE8041600`=0x1dc vs `0xE8042600`=0x1b2.
- **Resolution direction (reporter comment, Allegro-confirmed):** Prasanna cross-checked with Stephane (Allegro): *"the field description refers to the number of bytes processed by the core"* — i.e. per-core processed bytes, NOT total output bitstream size. So core0≠core1 is **EXPECTED**; label `burst_found_documentation` → likely a **documentation/spec-description fix, not a HW defect.**
- **ECHO note:** likely benign (doc clarification). Low silicon impact. Still New/open — final disposition pending doc update + formal Allegro response (reporter says "formally raised query with Allegro"). **State: BLOCKED on off-disk Allegro `encoder_specification.pdf` (STAT_000 field semantics).**
- **ATLAS bounded sourcing (arch-atlas):** literal register "STAT_000" does NOT exist on-disk by that name (Allegro encoder regspec `vcu2_0_alg_encoder_0_regs.h`, BASE 0xE8040000, uses REG_TOP_/REG_MCU_/REG_AXI_). On-disk per-core structure: REG_TOP_004 CORE0/CORE1 reset-enable (bit16/17); REG_TOP_016/017 = CORE0/CORE1 IRQ-COUNT (DEFVAL 0x0); REG_TOP_000 = IDENTIFICATIONNUMBER RO 0x30ab6e51 (not per-core). Three banks per enc instance (alg_encoder_0 @0xE8040000, _1 @0xE8041000, _2). **Disposition HINGES on STAT_000 type:** if config/ID → cores must match → mismatch anomalous; if live-status/count → per-core divergence EXPECTED/legal (cores process separate slices/streams per S1 §1.11.4.1 ULL). Only resolvable via off-disk `encoder_specification.pdf`. **Route Ledger:** fetch STAT_000 logical field def — config/ID (match-required) vs status (may-differ), valid read/snapshot window, and whether spec asserts core0==core1. No mismatch=bug conclusion drawn.

### EDT-1095022 — "[BURST][VCU2] Missing information for pitch configuration for Reconstructed buffer for 12bit depth" — RANK 5, CLOSED (doc)
- **Status:** Closed (Fixed). **Assignee:** Galagali, Abhay. **Labels:** burst_found_documentation. Created 2026-01-22, Updated 2026-07-23.
- **Trigger/symptom:** `Enc1RecLumaPitch`/`Enc1RecChromaPitch` fields of `REG_CMD_ENC1_128` and `REG_CMD_ENC1_129` specify pitch rules for 8-bit (multiple of 256) and 10-bit (multiple of 320) reconstructed-buffer depths, but **12-bit depth pitch restriction was not documented** — reporter asks whether unrestricted or missing.
- **Resolution (verbatim):** Galagali: *"Allegro clarified thru ticket [allegrodvt 6412]. Description updated accordingly."* Then: *"Fixed. Pushed to LIVE on 04/21/26. **Live CL#62082349.**"* Verifier assigned (Anu Dharshini).
- **ECHO note:** DOCUMENTATION fix (missing 12-bit pitch spec added), not a HW/RTL defect. **Concrete CL#62082349** (from Jira comment text — most Jira metadata is tool-blocked, this one was in a comment). Authority = Allegro ticket 6412 (off-disk).
- **DISPOSITION (Sentinel-confirmed 2026-08-12): `DOCUMENTATION_BUG` — historical, SCOPED TO THIS ISSUE.** Reporter identified missing 12-bit pitch documentation only; NO hardware symptom / reproducer / mismatch alleged; remedy = documentation clarification. **Preserved caveats:** Allegro 6412, the attachment, and CL 62082349 were **NOT opened** by Echo (author-asserted); target applicability and implementation correctness are **NOT established.** This is the one lead at a scoped-terminal disposition; all others remain EVIDENCE_PENDING/BLOCKED.

### EDT-1094741 — "[VCU2] IR waiver in low latency encoder fix db" — RANK 3, CLOSED (EMIR waiver, ties to EOF fix)
- **Status:** Closed. **Assignee:** Bhonge, Shashank. No labels. Created 2026-01-13, Updated 2026-02-03.
- **Content:** IR-drop / EMIR waiver request on `vcu2_core` **"with the latest eol and eof fix db"** — i.e. the physical-design DB that carries the EOL/EOF pulse fix (EDT-1094051 family). Two IR violations flagged (both encoder cores):
  - `g_core_num_0_...i_vcu2_enc_core_top/...PNR_PLACE_OPT...` BUFFSKRD18... IR=89.1mV, setup 43ps, hold 28ps.
  - `g_core_num_1_...i_vcu2_enc_core_top/...PNR_ROUTE_OPT1...` DCCKBEMM... IR=84.1mV, setup 43ps, hold 132ps.
- **Resolution (verbatim):** Bhonge: *"Approving waiver."* Checklist authority: Confluence **XPS/603256108 "VCU2 EMIR checklist and Release details."**
- **ECHO note (conclusions weakened per Orion/Sentinel):** **author-approved physical-signoff (IR-drop/EMIR) disposition — no functional failure identified in the retrieved record.** Does NOT independently imply low silicon risk: positive STA slack + an approved EMIR waiver are insufficient without voltage/temp/activity/corner + signoff criteria + the underlying EMIR analysis (NOT in hand). Metrics as reported: IR 89.1mV (core0, BUFFSKRD18, setup 43ps/hold 28ps), IR 84.1mV (core1, DCCKBEMM, setup 43ps/hold 132ps) — thresholds/corner/voltage NOT stated in record. Approval authority: Bhonge, Shashank ("Approving waiver"). Attachments: heatmap PNG NOT inspected. Expiration/conditions: none stated. Linked fix/CL: none in record. Functional-sim/silicon evidence: NONE. **Bounded EOF-thread significance:** record references vcu2_core "with the latest eol and eof fix db" on both enc cores — a textual reference the fix DB was in the PD run, NOT proof of correct implementation. Confluence EMIR checklist XPS/603256108 = coverage/exclusions lead. **EVIDENCE_PENDING** (retained per Sentinel gate).

### EDT-1096525 — "T3: VCU2 Isolation Waiver" — RANK 4, FIXED (isolation waiver; ties reset/iso family + T3-open)
- **Status:** Fixed. **Assignee:** Korra, Srinivasa Rao. **Labels:** FCV, T3. Created 2026-03-31, Updated 2026-04-19.
- **Content:** during L40/L80 verification the isolation checker was improved; the enhanced checker surfaced **new VCU2 isolation mismatches on top of the T40 waiver** on **T3**. Waiver approval requested (reporter Hemang).
- **Prior signoff (verbatim, Galagali):** *"For T50 VCU2 we had done this exercise and signed-off. This was tracked thru **EDT-1066841**. VCU2 tile remains same."* Then: *"VCU2 Isolation strategy has been successfully tested in T50 - no issues reported ... func paths look fine"* (review comments in "VCU2_T3_Isolation_Miss_Match - Abhay Review Comments.xlsx").
- **Disposition (verbatim, Korra):** *"we can use same waivers as T50. Highlighted are scan data-signals; there is no active state for these == no hard-clamp-value requirement. It's okay to clamp them '0' or '1'. Fabric drives a non-configured value of '1' ... scan_en gets a '0' clamp."* DFX signals reviewed.
- **ECHO note (conclusions weakened per Orion/Sentinel):** **author-approved isolation-signoff WAIVER (Fixed) — no functional failure identified in the retrieved record.** Affected: T3, VCU2 tile; violations surfaced by an improved isolation checker (L40/L80 cycle). Reported disposition: flagged mismatches are **scan data-signals + DFX signals** described as "no active state == no hard-clamp-value requirement" (OK to clamp 0/1; fabric drives '1'; scan_en '0' clamp). Approval authority: Galagali (review) + Korra ("use same waivers as T50"). Prior baseline: T50 signoff **EDT-1066841** (reporter-asserted "VCU2 tile remains same"). Attachments (review XLS "VCU2_T3_Isolation_Miss_Match - Abhay Review Comments.xlsx") NOT opened. Expiration/conditions: reuse-T50; no expiry stated. Functional-sim/silicon evidence: NONE in record. **Do NOT assert this is "the T3 propagation item Trace flagged"** — no exact linkage established; both merely concern T3, which is not proof of the same item. **Bounded relevance to EDT-1098410:** the record's own framing treats isolation as SIGNAL clamping (scan/DFX), which is *consistent with* (not proof of) Atlas's isolation≠register-reset ruling. **Route Ledger:** EDT-1066841 (T50 iso signoff baseline). **EVIDENCE_PENDING** — "scan/DFX-only, low risk" is the REPORT's claim, not independently verified.

### EDT-1066841 — "T50: VCU isolation waiver" (baseline for EDT-1096525) — Closed
- **Status:** Closed. **Assignee:** Galagali, Abhay. **Labels:** FCV, T50, **non_rtl_issue**. Created 2024-01-09, Updated 2024-05-03.
- **Content:** reporter (Hemang) saw isolation clamp + reset mismatches for attached signals; waiver requested. Resolution (verbatim, Galagali): *"if_npi_regs_status.mmd_npi_pcsr_control[6]: The latest UPF does not include this pin in ISO strategy ... get the latest run data"*; *"we can waive **noc_vcu2_scan_chnl_in[11:0]**"* (per Shreel); *"we can waive **NOC NMU and NSU inputs to VCU2**"* (per Lizhi). Reporter: "Waiver approved. Testcase passed with waiver."
- **ECHO note:** the authoritative **T50 isolation-waiver baseline**. Labeled **non_rtl_issue** — the mismatches were checker/UPF-strategy artifacts (NOC scan channel + NMU/NSU inputs), waived as non-functional. Author-approved (email-thread evidence, not opened). Confirms the isolation-strategy waivers are on **NOC/scan/DFX signals**, consistent with (not proof of) Atlas's isolation≠register-reset ruling. EVIDENCE_PENDING on target.

### Confluence XPS/603256108 "VCU2 EMIR checklist and Release details" (v50) — physical-signoff/release authority
- **Content:** EMIR signoff runs + waiver/release pointers for vcu2_core / vcu2_enc_core_top / vcu2_dec_top. Review EDT **EDT-1069571**; waiver EDTs **EDT-1073792** (vcu2_core/enc, ECO_9/10) and **EDT-1094741** (vcu2_core 14-ECO). Waiver Details: vcu2_core Thermal-vless scan-capture 1IR(84.5mV) / 1IR(85.3mV) → EDT-1073792.
- **⚠ LINEAGE FINDING (D1/D7-relevant):** every release/build pointer on this page targets block **`vcu2_v1_6t0_block`** (e.g. `/proj/release/release/everest/v1/**vcu2_v1_6t0_block**/vcu2_core/FCP28/...`, T40 TWF `..._func_ELEC_ff1_ELEC_100_rct_emr_a.twf.gz`). **The physical-signoff/release evidence is on the `6t0` block; the RTL snapshot Echo has is `vcu2_v1_7t`.** This is concrete evidence the **6t0→7t** step is real at the release layer, and that EMIR/release signoff captured here is for **6t0/T40** — NOT necessarily the 7t snapshot or the (unfrozen) audit target. Reinforces that target applicability is UNRESOLVED (SENT-001) and that 6t0-era signoffs must not be assumed to cover 7t. Attachments (EMIR checklist XLSX, IR/slew/spef rpts) NOT opened.

## 7. Historical leads / pre-candidate hypotheses (from EDT text-search, NOT dispositioned, NOT candidates)

**BOUNDED-QUERY INVENTORY (NOT completeness):** one JQL `project in (EDT, PVS) AND text ~ "VCU2" ORDER BY updated DESC`, run ~2026-08-12. **Reported total = 385; only the top 25 by `updated` were returned and inspected. The remaining ~360 were NOT inspected.** Completeness limits: single alias set (`"VCU2"` text match only — misses `VCU 2`/`MMD-VCU`/block-token variants), sort=updated-DESC (older issues buried), projects limited to EDT+PVS (excludes CR and others), Jira 429 rate-limiting constrained follow-up queries. **Absence of an issue from this top-25 window proves nothing** and cannot support any no-history conclusion. These are historical LEADS / pre-candidate hypotheses only — none promoted to "candidate" until target applicability, fix presence on the supplied RTL, and waiver status are each established. No target applicability inferred.

- **Sync EOF cluster (PRE-CANDIDATE / EVIDENCE_PENDING):** EDT-1094051 (Closed) / EDT-1096956 (Assigned) / EDT-1094076 (Closed) — three issues all titled *"Sync EOF should last exactly one clock cycle."* Mixed statuses (one Assigned/open, two Closed). **Similar titles + mixed statuses establish only a historical cluster** — NOT proof of duplicate/reopen/partial-fix. **Possible duplicate/reopen/incomplete-fix pattern requiring exact links, chronology and fix evidence** before any such claim. See §6b EDT-1094051 — NOTE (E10-R): live-EOF + LLP_MODULE=0 + no-narrowing DOES exist on disk, but only in three **simulation-harness** wrappers (unsynthesizable XMR into vcu_top_wrapper.i_glbl_intf), not product builds — so no PRODUCT exposure is demonstrated (grounds = sim-harness, NOT tie-low; Prover's tie-low reading retracted, burden-shift withdrawn), and none is refuted for the target product build. Remaining = real Jira issue (Ledger Rev-B: PL/FBW-owned, T40/T50 fixed, T3 open) + open product-config question (E11 packaging metadata + D1/D2). Historical lead.
- **EDT-1093413** [BURST][VCU2][Encoder] *"bytes in encoded bitstream reported by STAT_000 register of core 0 and core 1 differs"* — **New** (open). Encoder **core0/core1 mismatch**. → Breaker/Trace lead.
- **EDT-1098410** *"VCU2: Reset and control registers retain state after asserting MMD_NPI_PCSR_CONTROL[24] (ctrl_iso_1_mmd_pl)"* — **Assigned** (open). **Reset/isolation state-retention** — pairs with EDT-1069519 reset history and EDT-1096525 "T3: VCU2 Isolation Waiver." → Trace/Breaker lead.
- **EDT-1095022** [BURST][VCU2] *"Missing pitch configuration for Reconstructed buffer for 12bit depth"* — Closed. Buffer/format.
- **EDT-1094741** [VCU2] *"IR waiver in low latency encoder fix db"* — Closed. **Waiver** in low-latency encoder.
- **EDT-1096525** *"T3: VCU2 Isolation Waiver"* — Fixed. **Isolation waiver.**

---

## 6b. EDT-1094051 "Sync EOF should last exactly one clock cycle" — HIGH-PRIORITY PRE-CANDIDATE (EVIDENCE_PENDING)

**Jira (original record opened):**
- Status: Closed. Assignee: Erusalagandi, Srikanth. **Labels: `fixed_in_imt50`, `IMT_baseline_difference`, `security_review_closed`, `security_review_inprogress` (last two are contradictory — flag).** Created 2025-12-15, Updated 2026-07-31.
- **Requirement source (verbatim, from Jira Description field, reporter):** *"As per VCU2 specification, sync_eol ... one clock cycle pulse and sync_eof ... at least one clock cycle pulse ... But **Allegro has clarified that both sync_eol and sync_eof pulses should last exactly 1 encoder clock cycle.** Encoder clock is 950 MHz and PL clocks can't be that high ... sync_eof generated by PL DMA Engine IP like Framebuffer Write running ~300 MHz won't be accurate."* Cites Allegro tickets **5011 & 6213** — **AUTH-GATED off-disk references (tickets.allegrodvt.com), NOT opened, NOT authoritative until retrieved**. Attachment `image-2025-12-15-18-09-12-922.png` (waveform, not inspected).
- **fixVersions / affectedVersions / issue-links / CL / backport / revert / waiver: NOT RETRIEVED — TOOL LIMITATION.** `search_xilinx_jira` (both get_issue and field-list search modes) does not surface these fields in output; only summary/status/assignee/created/updated/labels/description/recent-comments are exposed. These remain **EVIDENCE_PENDING**; require Jira UI/API-with-fields or Ledger. Do not infer.
- **Verification (verbatim comments):** driven at 330/300/150/75/37.5 MHz; assertions check single-clock eof pulse; src_sync register `0xE8040060` = `0x13FF` pass / `0x33FF` fail; *"Found the Frame Buffer Index is rolling once per frame and has been fixed."* Presilicon tested 320x240 with "T50 Fix."

**Supplied RTL finding (implementation evidence, supplied snapshot — NOT silicon proof):** Two files exist: `vcu2_int_wrap/rtl/vcu2_int_wrap.sv` (base, 5433 lines) and `vcu2_int_wrap_with_fix.sv` (5432 lines). Per Prover/Orion report (not yet Sentinel-accepted): **filelist reportedly selects the base path** at :70; `_with_fix` **reportedly non-compiled**. *Compiled-closure status is Trace/Prover-reported pending Sentinel's closure audit.*
- In Trace-reported analyzed **base path `vcu2_int_wrap.sv`**: the EDT-1094051 **EOF** rising-edge pulse logic is **COMMENTED OUT** — L737-739 `/* ... sync_eof_sync_d1 / sync_eof_sync_re */`; L2228 `//.sync_eof({...sync_eof_sync_re...})` commented; **L2229 ACTIVE** wires `.sync_eof({vcu2_enc_sync_eof_sync_with_clk...})` = **level-synced signal, NOT a one-cycle rising-edge pulse.**
- By contrast, **EOL** rising-edge pulse IS active in base (L4959-4960 `assign vcu2_enc_sync_eol_sync_re = sync_with_clk && !sync_d1`).
- In non-compiled **`_with_fix.sv`**: the EOF fix is **live** (L737 `sync_eof_sync_d1/re` declared uncommented; L2228 `.sync_eof({sync_eof_sync_re...})`; L4979+ edge-gen present).

**ECHO note — STRUCTURAL OBSERVATION (not resolved intent/history). Reframed after Prover E10 + local RTL; earlier "dropped/superseded" framing superseded, and the intermediate "RELOCATED/config-gated exposure" framing also QUALIFIED per Prover E10:**
- **Structural fact (neutral wording):** the PL wrapper (`vcu2_v3_0_rfs.v`) contains configuration-gated pulse narrowing; the core wrapper (`vcu2_int_wrap.sv`) does not. This is a **structural observation — it does NOT by itself establish that PL placement replaced the core fix, nor rule out supersession/revert/missed-integration.** Resolving intent needs target hierarchy/config + change history (unavailable: no p4, D1).
- Generate structure (echo-verified `vcu2_v3_0_rfs.v` L1373-1394): `!ONE_INSTANCE && LLP_MODULE`→sync-module narrowing; `!LLP_MODULE`→raw pass-through; `ONE_INSTANCE`→raw pass-through. **Module DEFAULTS `ONE_INSTANCE=0/LLP_MODULE=1` (L86-87) are defaults ONLY — instantiated parameters may override.**
- **Wrapper analysis — E10-R (Prover self-corrected after Trace challenge; four wrappers):** Prover's instantiation-override params stand verbatim (override wins over the `vcu2_v3_0_rfs.v:86-87` module default 0/1): **LLM_wrap `design_1_vcu2_0_0.v:890-891` = 0/1; enc_wrap & dec_wrap `:586-587` = 0/0; a FOURTH wrapper `vcu2_vivado_wrap:882-883` = 0/0** (initially missed). BUT the tie-off reading was WRONG and is **WITHDRAWN**:
  - `vcu2_v3_0_rfs.v:956` in enc_wrap / dec_wrap / vcu2_vivado_wrap drives `.vcu2_enc_sync_eof(vcu_top_wrapper.i_glbl_intf.vcu2_enc_sync_eof)` — an **unguarded cross-module reference (XMR)**. Only `LLM_wrap:956` uses the narrowed `{eof_pulse_path1,eof_pulse_path0}`.
  - So the `1'b0` **sits on a DANGLING wrapper port, NOT the core's EOF driver** — and **LIVE EOF + LLP_MODULE=0 + NO NARROWING DOES exist on disk** (in the three XMR wrappers). The earlier "no live non-narrowed path on disk / tie-low" framing is RETRACTED (E10-R). **Burden-shift sentence ("burden sits on anyone asserting such a config exists") is DELETED / withdrawn by Prover.**
- **What SURVIVES, on different grounds (record the REASON, not just the conclusion):** those three XMR wrappers are **SIMULATION HARNESSES** — an **unsynthesizable XMR into `vcu_top_wrapper.i_glbl_intf` is NOT a customer/product build.** Therefore **no PRODUCT live raw-pass-through EOF is demonstrated** — but this rests on "these are sim harnesses," NOT on "the port is tied low." Conclusion unchanged (no product exposure demonstrated on disk); grounds corrected.
- **Scope caveat (Orion):** this covers the **four primary on-disk wrappers**; **backup trees (`*_bkp`, `rtl_11may_bkp`, `rtl_25may`) are UNAUDITED.** Not an all-on-disk claim.
- **Ledger Rev-B (PL/FBW ownership + CLs 6493847/6493947, netlist ECO, T40/T50 fixed / T3 open) is preserved SEPARATELY as Ledger-reported, UNAUDITED by Prover — not erased, not merged into Prover's wrapper analysis.**
- **Narrowed remaining question:** does any SUPPORTED PRODUCT config combine a **live EOF source** with `LLP_MODULE=0` (no narrowing)? On disk, live-EOF + LLP_MODULE=0 + no-narrowing DOES appear — but only in the **three XMR simulation-harness wrappers** (unsynthesizable XMR into `vcu_top_wrapper.i_glbl_intf`), which are **not product builds**. So **no PRODUCT exposure is demonstrated on disk, and none is refuted** — the question is genuinely open, NOT burden-shifted (Prover withdrew the burden-shift). **The governing question remains checklist item 6 — which wrapper/config is active for the target PRODUCT build — NOT a supersession/regression axis.** Resolvable only with packaging metadata (E11: component.xml/xgui Tcl, off-tree) + target tuple (D1/D2).
- **Prover E11 (PROVER-DIRECT, D2 evidence dependency — NOT a negative finding):** the exposure question reduces to whether `LLP_MODULE`/`ONE_INSTANCE` are user-selectable in the PACKAGED Vivado IP. Prover searched the tree: no `component.xml`/`*.xit`/`xgui/*.tcl` for VCU2, no `LLP_MODULE` in any xml/tcl; `component.xml` exists only under `rtl/hnic_v1_7t/`, none for VCU2. **VCU2 Vivado IP packaging is ABSENT from this workspace → the supported-config question cannot be closed from the supplied tree alone.** Evidence dependency (folded into D2), NOT "no exposure found."
- **ECHO E12 — off-tree packaging authority LOCATED (Confluence; Prover-VERIFIED independently). Scope = PORT gating only:** **VCU2 IP Wizard Document**, Confluence space TDL, page **753167822** (version 23). Prover re-read the page directly (confluence_get_page works; the cloud_atlassian 401 is JIRA-ONLY) and confirmed verbatim:
  - User Parameters List: `C0_ENC_ENABLE_LOW_LATENCY_MODE` — **Default `False`, Range `True/False`, "Encoder Low Latency Mode Configuration."** User-selectable, defaults OFF.
  - Port table: `c0_vcu2_enc_sync_eol[1:0]` and `c0_vcu2_enc_sync_eof[1:0]` — **"Enabled when `C0_ENC_ENABLE_LOW_LATENCY_MODE == True && C0_ENABLE_ENCODER == True`."**
  - Encoder Options → Other Configuration (Prover's stronger read): *"Low Latency Mode(Encoder)<TRUE/FALSE>: User can enable **sync IP** with this switch"* and *"FALSE: Disables **sync IP** support."* → the switch binds to a **sync IP block**, not merely port visibility (stronger than the port table alone).
  - **What E12 establishes:** a live EOF **PORT** is enabled ONLY when LOW_LATENCY_MODE=True. When False (default), the sync_eof port is not enabled / sync IP disabled → no live EOF PORT.
  - **What E12 does NOT establish (hold the line — Prover):** the page **NEVER names `LLP_MODULE`** and **never names `vcu2_v3_0_2_lc_sync_module`.** Reading the IP-Wizard "Low Latency Mode" switch / "sync IP" as the RTL `LLP_MODULE` param or the `lc_sync_module` narrowing block is **INFERENCE, not documented.** E12 gives **PORT gating**; the **NARROWING-branch gating remains INFERRED** until packaging metadata (component.xml / xgui Tcl, per E11) turns up. Do NOT record "low-latency corresponds to the LLP narrowing branch" as documented.
  - **E12 does NOT touch E7:** E7 (core-internal `vcu2_int_wrap.sv`: EOL edge-detect live :4959, EOF edge-gen commented :4981-4992, :2229 carries the level) is a PL-wrapper-independent core fact. Low-latency=True is precisely the case where that core path runs. **E7 asymmetry stands, untouched by E12.**
  - **Residual caveats:** (1) IP Wizard v23 applicability to exact target SKU/stepping/release = D1/D2; (2) `ONE_INSTANCE` absent from the page entirely — that axis unconfirmed; (3) LLP_MODULE↔LOW_LATENCY_MODE and sync-IP↔lc_sync_module mappings INFERRED, pending packaging metadata. **Net: PORT-enable co-gating documented for sync_eol/eof; narrowing-branch mapping + ONE_INSTANCE + target applicability remain EVIDENCE_PENDING.**
- **Prover's confirming asymmetry (carried):** in `vcu2_int_wrap.sv` the EOL edge-detect is ACTIVE (:4949-4960, EDT-1059312) while the structurally identical EOF generator directly below (:4980-4992) is COMMENTED — same file/style/adjacent, same Allegro exactly-1-cycle requirement per the Jira description. Real asymmetry, worth carrying.
- **EOL/EOF lineage (echo, EDT-1059312 opened):** EDT-1059312 "RTL Planned Changes" (Closed; Galagali, Abhay; Created 2023-09-08, Updated 2024-01-09) is the DOCUMENTARY ORIGIN of the EOL pulse-sync, verbatim: *"Synchronized versions of **sync_eol** should be made into a pulse and connected to ENC logic."* It covers **sync_eol only — NOT sync_eof.** Verifier (Prasanna): *"checked the synchronizers on the sync_eol signal ... checked in waves."* → This explains the asymmetry's ROOT: the **EOL** pulse was a 2023 planned change (active in base); **EOF** pulse-shaping came ~2 years later via EDT-1094051 (2025-12) and landed config-gated on the PL side. Different issues, different eras, different placement — NOT a single fix partially applied. (EDT-1059312 also bundled SMID-bypass, NMU/NSU parity interrupts, NPI addr-size port; linked EDT-1063082 for poison signals — separate leads.)
- **UNKNOWN (will not guess):** resolved `ONE_INSTANCE`/`LLP_MODULE` values for the target build; whether any non-LLP or one-instance config is a supported product config; `_with_fix` retention reason (needs p4/source history, D1). mtimes (base 2026-06-18, _with_fix 2026-03-12) = sync time in unpinned shadow workspace, **NO evidentiary weight** — no chronology built on them. **Disposition: `EVIDENCE_PENDING`. This lead is HELD by Orion/Sentinel. No handoff to Prover or Breaker (both HELD). Prerequisite evidence-gathering only, by Trace/Atlas/Echo/Ledger (edge-gen trace of the analyzed cone, interface authority, Jira fields) — no adversarial construction or authority ruling is requested or implied. Retention reason for `_with_fix` UNKNOWN.**

> **SELF-FALSIFICATION (record explicitly, with corrected grounds — E10-R):** The broad "on-disk failing EOF configuration / dropped-or-relocated fix" lead is **weakened, but via corrected reasoning.** E10-R (Prover, after Trace challenge): the enc/dec/vcu2_vivado_wrap instantiations DO override to `LLP_MODULE=0` (no narrowing) and DO have a live EOF, so live-EOF+no-narrowing **exists on disk** — the earlier "tie-low / none on disk" claim is RETRACTED. **What defeats product exposure is NOT a tie-off** but that those three wrappers are **simulation harnesses** (unsynthesizable XMR into `vcu_top_wrapper.i_glbl_intf`, not a customer build). So: **no PRODUCT live raw-pass-through is demonstrated** (grounds = sim-harness, not tie-low), **but neither is it refuted for the target product build** (needs E11 packaging metadata + D1/D2 target). No active PRODUCT failing config demonstrated; the historical Jira issue is real (Ledger Rev-B: PL/FBW-owned, CLs 6493847/6493947, T40/T50 fixed, T3 open). Backup trees unaudited. Do not imply an active product failing config, and do not imply the question is closed reassuringly.

> **LEDGER Rev B primary-source chain (via Orion, 2026-08-12) — NOT a VCU-local regression:** Ledger found the fix chain: **EDT-1094076** = PL workaround / Framebuffer-Write (FBW) ownership, **CL 6493847 (HEAD) + CL 6493947 (REL)**; **EDT-1096956** = netlist-ECO delivery; **T40/T50 have fixed evidence; T3 propagation OPEN.** This **CONFIRMS the fix lives in PL soft-IP / FBW ownership** (consistent with E12: source+narrowing co-gated in the LLP PL path) and that the resolution was carried via CL + netlist ECO for T40/T50. **Remove any "VCU-local regression / dropped core fix" implication** — the EOF pulse-shaping is a PL-side / FBW-owned concern, tracked and delivered per the above CLs, not a VCU-core regression. Remaining open: T3 propagation; and target-tuple applicability (which stepping the audit target is) stays UNRESOLVED (SENT-001). Jira-field/CL specifics are Ledger-reported (I did not independently pull CLs — my Jira tool cannot expose CLs; see D-tool-limit). Jira labels `IMT_baseline_difference` + `fixed_in_imt50` are **issue METADATA only — NOT proof** that this supplied closure or silicon includes the fix; they must be reconciled against this snapshot's actual compiled EOF logic (which shows the EOF pulse-fix commented in base). **Contradictory labels `security_review_closed` + `security_review_inprogress` co-exist** — cannot explain from available fields; flag to Ledger/Sentinel (possible workflow-state artifact or unreconciled parallel review).

**Evidence-tier separation (per Sentinel EOF-SYNC-01):**
- *Reporter expectation:* the exactly-1-encoder-clock requirement + ~300 MHz PL-source problem (Description, reporter).
- *Design-owner intent:* edge-detect/sync3 fix (code comments `//abhayg: EDT-1094051`; author "abhayg" ≠ Jira assignee Erusalagandi — design vs verify split).
- *Executed evidence:* verification comments (Ammula/Kalluri/Erusalagandi) — presilicon 320x240, multi-freq assertion checks, src_sync `0xE8040060` `0x13FF`/`0x33FF`, frame-buffer-index roll fix. **These are reported Jira dispositions, not independently reproduced.**

## 7b. Off-disk spec references embedded in the DID (Orion request: Allegro datapath specs)

Extracted from `Telluride_DID_1.5_VCU.docx` `word/_rels/document.xml.rels` (the DID's own embedded hyperlink targets — NOT RAG). These are **DID-embedded candidate reference pointers**, NOT authoritative evidence — none is confirmed until the original file is opened, versioned, and applicability-checked (D7/target authority open):

| Reference | URL (verbatim from DID) | Notes / access |
|---|---|---|
| **Decoder Specs folder** | `https://xilinx.sharepoint.com/:f:/r/sites/eng/processing solutions/everest/des_ver/Shared Documents/Modules/VCU/Decoder Specs` | SharePoint folder — **not fetchable via WebFetch (auth)**. Likely holds Allegro decoder datapath spec. Version UNKNOWN — must open folder. |
| **Arch Specs (released) — MMD** | `https://xilinx.sharepoint.com/sites/eng/fdst/everest/arch/Shared Documents/Arch Specs (released)/Telluride/MMD/Everest_MMD_VCU_Arch_Specs_1.0.pdf` | Points at **v1.0** under "(released)". Confirms DID references v1.0. v1.2 (on disk) not linked here — D7 open. Auth-gated. |
| **Allegro DVT ticket 4285** | `https://tickets.allegrodvt.com/issues/4285` | External vendor (Allegro) tracker — encoder/decoder IP issue. Auth-gated, off-network. |
| **Allegro DVT ticket 4858** | `https://tickets.allegrodvt.com/issues/4858` | External vendor (Allegro) tracker. Auth-gated. |
| **VCU APM Specification** | `http://xinc/ppg/processing solutions/Montana/fe/Modules/VCU/Eng Spec/VCU_APM_Specification.docx` | Legacy Montana-path eng spec (internal xinc). |
| **Everest DID SysMon** | `amdcloud.sharepoint.com/.../Everest_DID_SysMon.docx` | Related SysMon DID. |
| **VCU DFX folder** | `amdcloud.sharepoint.com/.../Telluride/VCU/DFX` | DFX design docs. |

**EDT issue links embedded in the DID** (DID-embedded cross-reference leads — NOT authoritative, the linked issues are unopened): EDT-1056613, EDT-1059821, EDT-1066811, EDT-1069146, **EDT-1069519**, **EDT-1072959**. → The first four are NEW leads the DID cites; unopened, pulling next (429 backoff).

**Access status:** All SharePoint/Allegro links are **auth-gated / off this network** — WebFetch cannot retrieve (would fail/redirect). Reporting exact URLs + versions per Orion; original retrieval requires the user's SharePoint/Allegro session. No RAG substitution.

## 8. Searches performed (this session)

1. `sage.search_docs "VCU2 encoder decoder silicon escape regression defect"` → 5 hits (T50 VnC bring-up plans, MMD-VCU). *Leads only.*
2. `confluence_search "VCU2 Telluride Everest silicon escape errata"` → 10 pages (XPS/XSIC). Opened **793871409** (Everest hub, 140K, scanned for errata/Jira pointers).
3. `search_xilinx_jira` JQL `project in (EDT, PVS) AND text ~ "VCU2" ORDER BY updated DESC`, max_results=25, ~2026-08-12 → **reported total 385; only top 25 by `updated` returned/inspected; remaining ~360 NOT inspected.** Single-alias/sort/project-scoped, 429-limited. Not completeness.
4. `get_issue EDT-1069519`, `get_issue EDT-1072959` → full records (above).
5. Local DID `Telluride_DID_1.5_VCU.docx`: extracted Revision History, Open Issues table, §4.2 Arch Spec pin, ABUS/DPSTx2 + decoder/IPB passages.

## 9. Open blockers / next actions

- **BLOCKER:** frozen target tuple (SENT-001) — no no-history / candidate-mapping verdicts until resolved.
- **BLOCKER:** exact P4 changelist of target snapshot (p4 not in PATH).
- **PENDING (429 backoff):** EDT-1076244 (waiver state); EDT-1093413/1098410 full fields; Sync EOF cluster full fields + links; affected/fixVersions for 1069519/1072959; duplicate/link graphs.
- **TODO:** confirm Arch Spec v1.2 formal-release status vs DID-pinned v1.0 (SharePoint "Arch Specs (released)").
- **TODO:** map DID-closed decoder single-core/IPB + reset decisions to RTL presence in `vcu2_dec_top` / `vcu2_reset` on target snapshot.
