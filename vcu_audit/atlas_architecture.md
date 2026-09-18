# ATLAS — VCU Architecture Reconstruction (Intended Microarchitecture & Invariants)

Author: `arch-atlas` (principal VCU microarchitecture / design-review)
Status: **ARCHITECTURE MODEL (intent) — NOT completed investigation coverage.** Living document.
Cycle: 1 → revised per SENT-008 / Orion corrections.
Deliverable root: `/scratch/karthick/fonely/vcu_audit/` (canonical, per Orion).
Scope discipline: READ-ONLY. Architecture *intent* only. RTL end-to-end behavior → Trace; timing → Chronos; failure modes → Breaker; formal invariants → Prover; history/waivers/EDT → Ledger/Echo.

> **READER WARNING:** This document models INTENDED architecture from two documents + register-generated headers. It is NOT proof of silicon/target behavior. Every normative item carries an ALLOWED-STATE field. No high-risk flow here is terminal; all are architecture-predicted pending downstream evidence.

---

## 0. Source / Revision Manifest (reproducible)

Extracts copied into canonical root for stable `file:line` citation and spot-checking:

| Handle | Rendered source (absolute path) | SHA-256 (source) | Text extract in canonical root | SHA-256 (extract) | Extraction method |
|--------|-----------------|--------------|-------------------------------|---------------|-------------------|
| **S1** | `/everest/apex_pvs_nobkup/karthick/vcu_analysis/Everest_MMD_VCU_Arch_Specs_1.2 (1).pdf` (Version 1.2, 22 pp, ModDate 2024-06-22) | `8de61603…916d` (full: 8de6160331b9a309225cb7cf769456c8ad9f79839590c6f7d0d444cd5141916d) | `vcu_audit/vcu_arch_spec_1.2.txt` | `4e05cf65…6740` (full: 4e05cf6581c50867e7daf4c218fd79bba11f0d78b58e7b93d4235de0810f6740) | `pdftotext -layout` (poppler 0.86.1) |
| **S2** | `/everest/apex_pvs_nobkup/karthick/vcu_analysis/Telluride_DID_1.5_VCU.docx` (internal rev 1.5.1) | `a33ca7ae…77a7` (full: a33ca7ae2fbd20926fe2e8959c7dc8dfefe2a39f7c4504e1fe65ea134a1977a7) | `vcu_audit/vcu_did_1.5.txt` | `14057e6c…daf1` (full: 14057e6c4c8473aaf71521fd8b3b863ea315dcfb5f524c90c83c2f5a720ddaf1) | docx `word/document.xml`→text (regex tag-strip; **excludes embedded figures/objects**) |
| **S4′** | Byte-identity of RTL closure (Trace): manifest SHA-256 `5ec3e8a5748b78ce5d864a8ce928d0a0d693a62cb6d2a1bf219e2db3acbd3d53` (67 filelists, 2166 unique design files) | — | — | — | Trace `trace_rtl.md` |
| **S5** | On-disk RTL-generated regspecs (see §0.1) | — | (referenced in place) | — | — |
| **S6** | Workspace config: `…/VCU_VERIF_12MARCH/model_tags.cfg` (MD5 `409350585c5e88122d9d5f9414f94963`), `…/VCU_VERIF_12MARCH/.project_config` (MD5 `bc5eca97c834b12fdc35342260014af4`) — **ONE LEVEL ABOVE `shadow/`** | — | — | — |

Citations use: **S1 §x.y (PDF p N)** or **S1:LINE** (line in the copied extract) or **S2:LINE**; plus verbatim short quote. S5 citations are absolute RTL paths + `file:line`/`#define`. RTL structural facts attributed to Trace cite `trace_rtl.md` + file:line.

**Absence-search reproducibility:** grep is case-insensitive extended-regex (`grep -niE`) over the canonical text extracts (S1/S2) and S5 register headers. Absence claims are bounded to **searched text-extract/regspec corpus only** — extraction excludes embedded figures/diagrams/objects and all off-disk (SharePoint/Jira/XRDB) specs. "Not found in searched corpus" ≠ "does not exist."

**S2 EXTRACTION CAVEAT (per Ledger):** the S2 extract was produced by a plain `word/document.xml` regex tag-strip, which is NOT tracked-change-aware — it may merge inserted+deleted revision text or drop `w:ins`/`w:del` markup, and does not resolve rendered tracked-change state. A tracked-change-aware extraction supersedes this method where they differ. S2 line-number citations are stable against THIS extract (SHA-256 `14057e6c…daf1`); any citation touching revision-marked text should be re-confirmed against a tracked-change-aware render before being treated as final. Ledger also reports the DID XRDB link exists and prior empty-table claims are retracted — table-cell content in the DID may be richer than this flat extract shows.

### 0.1 S5 register-truth files (RTL-generated, MCP-independent)
Root `RR = /everest/psival_verif_nobkup/manish/VCU_VERIF_12_MARCH/VCU_VERIF_12MARCH/shadow/rtl/vcu2_v1_7t`:
- `RR/vcu2_slcr_regs/regspecs/vcu2_0_slcr_regs.h` (+ .svh/.xml/.rtf) — SLCR (instance 0 and 1 variants present).
- `RR/vcu2_core_top/vendor_ip/vcu2_decoder_regspecs/regspecs/…` and `…/vcu2_encoder_regspecs/regspecs/…` — Allegro core regs.
- `RR/include/vcu2_npi_regs_defines.vh` — NPI PCSR/PSR field bit positions.

**MCP status:** NONE (`claude mcp list` empty). Jira/XRDB/Confluence/GitHub/Docs-RAG UNAVAILABLE. All external links in S1/S2 UNVERIFIABLE here. S5 is the only register cross-check.

### 0.2 Applicability axes (kept SEPARATE — none collapse to "target-governing")
1. **Document identity mapping** — SUPPORTED family mapping (IDENT-01); NOT unconditional target-binding.
2. **Architecture version binding** — DOCUMENT REVISION-BINDING MISMATCH only (C-01): DID hyperlink → v1.0; supplied spec v1.2. Proves neither governs the target.
3. **Workspace/snapshot identity** — unpinned-looking config metadata; no runtime-resolution semantics proven (C-02).
4. **User-target applicability (SKU / stepping / feature-enable)** — UNRESOLVED on disk.

**ALLOWED COVERAGE STATES — canonical SENT-010 vocabulary (exactly one per invariant/sequence/conflict/gap/high-risk flow):** `NOT_STARTED` · `IN_PROGRESS` · `EVIDENCE_PENDING` · `BLOCKED` · `INVESTIGATED_NO_ISSUE` · `CANDIDATE_FOUND` · `PROVER_REVIEW_PENDING` · `COMPLETE`. This document references the full canonical set; states used here are chiefly EVIDENCE_PENDING (searched-corpus evidence exists, downstream Trace/Chronos/Breaker/Prover/history verification still required) and BLOCKED (cannot progress without off-disk/MCP authority — Jira, XRDB, Allegro specs, released-approval record, exact P4 changelist).

**DISPOSITION / TYPE labels (separate axis, NOT coverage states, NOT counted):** `SPEC_ARCH_CONFLICT` (document/spec-vs-spec or spec-vs-DID conflict — a disposition, e.g. C-01; the coverage STATE is still one of the canonical set, typically BLOCKED when authority conflict prevents progress) · document-binding disposition (a documentary fact about the docs themselves).

**DESCRIPTIVE MATURITY LABELS (separate axis, NOT coverage states, NOT counted):** `doc-supported` · `doc-only` · `regspec-corroborated` · `trace-reported-structural` · `document-binding-confirmed`. Nothing here is COMPLETE — no item has passed independent audit + Breaker/Prover.

---

## 1. Identity & Applicability Findings

### IDENT-01 — Everest-MMD-VCU ≡ Telluride-VCU ≡ VCU2 — coverage: EVIDENCE_PENDING; maturity: doc-supported (family mapping only)
- S1 §1.1 (PDF p4; S1:136-137): *"The MMD VCU is part of the Telluride family of products. The MMD VCU is sometimes referred to as VCU2, to distinguish it from the original Diablo (Zynq Ultrascale+) VCU."*
- S1 §1.1 (PDF p4; S1:133): *"…can replace GTY quad pairs along the right edge of an Everest (Telluride) SoC device."*
- S2 intro (S2:357): *"…detailed engineering specification of Telluride Video Codec Unit… based on the Everest (Telluride) MMD VCU architecture requirements."*
- **Allowed reading:** supported family identity mapping. **Forbidden reading:** unconditional target-binding equivalence (authority/applicability unresolved).

### C-01 — Architecture version-binding MISMATCH — coverage: BLOCKED (release/approval authority off-disk); disposition: SPEC_ARCH_CONFLICT (document-binding only, not target authority); maturity: document-binding-confirmed
- S2 References (S2:487) hyperlink URL-decodes to `Everest_MMD_VCU_Arch_Specs_1.0.pdf`; supplied spec is **v1.2**.
- **Allowed conclusion:** the DID's cited architecture baseline (v1.0) differs from the supplied spec (v1.2) — a document revision-binding mismatch. **Forbidden conclusion:** that either v1.0 or v1.2 governs the manufactured target (authority UNRESOLVED, BLOCKED on release/approval record).
- Consequence: v1.1/v1.2-only text is revision-specific (see per-invariant rev tags).

### C-02 — Workspace metadata unpinned-looking config only — coverage: BLOCKED (P4 changelist/SKU/stepping off-disk); maturity: document-binding-confirmed (SENT-006 corrected)
- Exact paths (S6; **one level ABOVE `shadow/`** — this is why Trace's depth≤4-under-shadow search missed them): `/everest/psival_verif_nobkup/manish/VCU_VERIF_12_MARCH/VCU_VERIF_12MARCH/model_tags.cfg:459` (MD5 `409350585c5e88122d9d5f9414f94963`): `library="vcu2_v1_7t_live" tag_kind="snapshot" tag_name=""`. `.../\.project_config:1-7` (MD5 `bc5eca97c834b12fdc35342260014af4`): Project=everest, Variant=v1, Major=6, Minor=1, ModelTagKind="snapshot".
- Empty `tag_name` on ALL peer blocks (cmt/blif0/isp2) → naming consistency, NOT resolution semantics. No "live-tip tracking" asserted. Major/Minor = PSSG workspace fields, not stepping.
- Independent SCM identity Trace can cite: `shadow/.icmconfig` (53 B) = `P4CLIENT=yadav+everest+v1+35`, `P4PORT=xhdicmsuper:1777` — no changelist, no tag, no SKU, no stepping. These metadata establish CONTENT+PATH only, NOT target provenance (consistent with Trace).
- Byte-identity reproducibility anchor (Trace S4′): closure manifest SHA-256 `5ec3e8a5748b78ce5d864a8ce928d0a0d693a62cb6d2a1bf219e2db3acbd3d53`.
- **BLOCKED:** exact P4 changelist; user-target SKU/stepping.

---

## 2. Control & Datapath Model (intended)

3-way separation (per Orion) — every element tagged **[ALG]** Allegro hard-IP internal · **[WRAP]** AMD integration wrapper · **[CFG]** selected config.

### 2.1 Top structure (S1 §1.6 PDF p10; S2 §3.1 block desc S2:531-704)
- **[ALG]** Encoder — AL-E200-E, DUAL-core, 4Kp60, H.264/H.265, RISC-V MCU (64KB RAM, 40-bit). L2 1152KB (S2:590 "1.21875 MB"), L1 168KB, MCU cache 21.875KB.
- **[ALG]** Decoder — AL-D300-S, SINGLE-core, 4Kp60, H.264/H.265/**JPEG**, RISC-V MCU. Decoder cache 24KB (S2:698).
- **[WRAP]** Interconnect (Arteris), SLCR, NPI slave regs, Reset block, SSC eFuse receiver, APM (S2:709 "five four" → resolved to 5: enc/dec AXI-M core + AXI-S + enc/dec AXI-M MCU), NoC feedthrough, 4× DFX, BISR-C, POR/BPD-POR, Abus/SysMon.
- **[CFG]** which features/instances enabled — UNRESOLVED.

### 2.2 Memory / datapath (S1 §1.9 PDF p11-12; S2:533, 640)
- **[ALG]** Enc: current + 0/1/2 ref frames → encode → bitstream to DRAM. Slice-level. BW ~6 GB/s @4Kp60 4:4:4 12b (L2 on; S1 §1.11.2).
- **[ALG]** Dec: mem-to-mem, bitstream + 0/1/2 ref → frame buffer. BW ~10-15 GB/s (S1 §1.11.3).
- **[WRAP]** AXI-M: 2×128b enc + 2×128b dec → 4 NMUs. Max outstanding rd=64/wr=64 (S1 §1.12 PDF p14; S2:2198). Incrementing bursts.
- **[WRAP]** AXI-S: 1×128b @167MHz; base `0xE8000000` (S2:702) — regspec-corroborated S5 `vcu2_0_slcr_regs.h`: `#define VCU2_0_SLCR_BASEADDR 0xE8000000`. Multi-tile via NMU remap (S2:702).

### 2.3 Address map (S2 §3.1.2 S2:2143-2200) — coverage: EVIDENCE_PENDING; maturity: doc-only + partial regspec-corroborated; see A-05
- NSU addr [21:20] selects APB slave: `00`=SLCR, `01`=Allegro Decoder, `10`=Allegro Encoder (S2:2153-2172). APB 20-bit per slave.
- regspec-corroborated: SLCR base `0xE8000000` and SLCR has a VERSION register (S5 `vcu2_0_slcr_regs.h`: `VCU2_0_SLCR_VCU2_VERSION @ +0x14`, DEFVAL `0x1`, 4-bit) — matches S2:713 "SLCR would have version register."

---

## 3. State-Machine Summaries (intended)

### SM-1: SSC eFuse Receiver — [WRAP] — coverage: EVIDENCE_PENDING; maturity: doc-only, regspec-corroborated (status bits)
Observer-only FSM, live while `ssc_efuse_en = en_glob & ~ghigh_b` (S2:1976). Captures 4×32-bit control packets (broadcast 6'h3f, SEC bit[20]=1, word index MSW→LSW; S2:1962-1967) → eFuse value; bits [40:37] enc/dec disable (S2:1986, Table 3 S2:1995-2010).
- regspec-corroborated: S5 `vcu2_npi_regs_defines.vh` has `PCSR_STATUS_FLD_MMD_SSC_EFUSE_VALID_DISABLE_IP0=26`, `…IP1=27`, `…RT_DISABLE_IP0=28` — per-instance eFuse status bits exist in RTL.
- Legal states: post-reset DISABLED (S1 §1.20 PDF p16; S2:1977). Enable ONLY on valid eFuse=0. Forbidden: use of eFuse outputs before valid flag (S2:2044).
- INV-SSC-02 recovery: lockout clears ONLY via `por_pl_b` (S2:2046).

### SM-2: POR / Pre-config / Startup — [WRAP]+PMC — coverage: EVIDENCE_PENDING; maturity: doc-only, regspec-corroborated (bit map)
Strict ordered NPI register protocol (§5 TS-1). regspec-corroborated bit positions (S5 `vcu2_npi_regs_defines.vh`): IPOR=bit30, INITSTATE=bit6, PCOMPLETE=bit0, SCAN_CLEAR_TRIGGER_[3:0]=bits2-5, MEM_CLEAR_TRIGGER_[3:0]=bits17-20, SLVERREN=bit21, CTRL_ISO_0..4 = bits23-27, BISR_TRIGGER=bit29; STATUS: PCSRLOCK=bit0, BISR_DONE=bit23, MMD_PWR_SUPPLY=bit25. These MATCH the DID startup writes (e.g. S2:1922 "vdu_npi_pcsr_mask[30]…vdu_ipor"; S2:2834-2835 IPOR mask/control).

### SM-3: GATED_CLOCK — [ALG] — coverage: EVIDENCE_PENDING; maturity: doc-only
`GATED_CLOCK[1:0]`: `00`=HW-gated (on start/completion), `01`=internal free-run, `1x`=disable (S2:558-568 enc; S2:658-668 dec). Per-engine.

### SM-4: Scan/Mem-clear/BISR — [WRAP]/DFX — coverage: EVIDENCE_PENDING; maturity: doc-only, regspec-corroborated (triggers)
Triggered via NPI PCSR (TMR). Int[10] non-maskable, asserts on trigger, self-clears on done+pass (S2:2217-2224). Mem-clear on NPI clock (EDT-1066811, S2:2805-2817).

### SM-5: Reset generation/distribution — [WRAP] — coverage: EVIDENCE_PENDING (Trace) — see C-03
Combines external (`pl_vcu2_raw_rst_n`, `pl_vcu2_spare_in[56]`=ENC, `pl_vcu2_spare_in[11]`=DEC, `pmc_por_rst_b`) + internal (`efuse_dis_enc/dec`, IPOR, INITSTATE) → per-block nets (S2 Table 10, S2:2687-2728; RDC S2:2736-2744).

---

## 4. Architecture Invariants (rev-tagged, state-tagged)

Columns: **ALLOWED/FORBIDDEN** = the invariant's permitted/must-never states (the invariant content). **Coverage state** = exactly one canonical SENT-010 state (here: EVIDENCE_PENDING or BLOCKED). **Maturity** = descriptive label (not counted). **EVID needed** = what closes it.

| ID | Invariant (ALLOWED / FORBIDDEN) | Source (reproducible) | Rev sensitivity | Coverage state | Maturity | EVID needed |
|----|----------------------------------|----------------------|-----------------|----------------|----------|-------------|
| INV-RESET-ISOLATION-01 | ALLOWED: reset ENC without disturbing DEC and vice-versa. FORBIDDEN: a single-engine hang forcing the other engine's reset. | S1 §1.6 (PDF p10; S1:424-425); S1 §1.5.6 (PDF p9; S1:396-402) enumerates Hard reset. | §1.5.6 enum = v1.2-NEW (S1:105); DID binds v1.0. | EVIDENCE_PENDING | trace-reported-structural (disjoint cones, supplied analyzed closure) | dynamic behavior, shared-resource isolation, target reachability, authority; Sentinel/Breaker/Prover review |
| INV-CLK-INDEP-01 | ALLOWED: independent enc/dec clocks, independent gate/rate-change. FORBIDDEN: one engine's clk change disturbing the other. | S1 §1.6 (S1:424-425); §1.5.6 (S1:398-399). | v1.2 sharpened. | EVIDENCE_PENDING | doc-only | Trace CDC |
| INV-DISABLE-01 | ALLOWED: post-reset DISABLED until eFuse=0. FORBIDDEN: enabled-by-default. | S1 §1.20 (PDF p16); S2:1977. | stable | EVIDENCE_PENDING | doc-supported, regspec-corroborated (SM-1 status bits) | Trace FSM |
| INV-SSC-02 | ALLOWED: valid only after 4 in-order words AND ssc_efuse_en de-asserted; lockout cleared only by por_pl_b. FORBIDDEN: eFuse-output use before valid; lockout clear by any other reset. | S2:2044-2046. | DID-detail | EVIDENCE_PENDING | doc-only | Trace FSM |
| INV-RESET-SEQ-01 | ALLOWED: release `pl_vcu2_raw_rst_n` only after all clocks stable; INITSTATE de-assert only after clocks stable. FORBIDDEN: reset release with unstable clocks (glitch). | S2:2731; S2:2784-2785. | DID-detail | EVIDENCE_PENDING | doc-only | Chronos |
| INV-RESET-RDC-01 | ALLOWED: read NPI data only after IPOR released; read IMR only after INITSTATE released; NPI intf-reset de-asserted before any reg read. FORBIDDEN: earlier reads. | S2 RDC table (S2:2752-2762). | DID-detail | EVIDENCE_PENDING | doc-only | Chronos |
| INV-NPI-INT10-01 | ALLOWED: int[10] asserts on scan/mem-clear trigger, self-clears on done+pass. FORBIDDEN: masking int[10] during SC/MC. | S2:2217-2224. | DID-detail | EVIDENCE_PENDING | doc-only, regspec-corroborated (trigger bits) | Trace/Breaker |
| INV-APB-01 | ALLOWED: SLVERR is the legitimate AXI→APB FIXED-burst response. FORBIDDEN: expecting normal completion of FIXED to enc/dec APB. | S2:701 (Arteris SLVERR); S2:2200 (Allegro ticket 4714). RTL: SLVERREN=bit21 (S5). | DID-detail | EVIDENCE_PENDING | doc-only, regspec-corroborated (SLVERREN) | Breaker semantics |
| INV-EFUSE-BOTHDIS-01 | ALLOWED: if BOTH enc & dec disabled → NO register access (SLCR+interconnect in reset). FORBIDDEN: driver reg access when both disabled. | S2:2819; reset-fanout S2:2712/2717. | DID-detail | EVIDENCE_PENDING | doc-only | Trace reset-fanout |
| INV-ADDR-48-01 | ALLOWED: 48-bit address honored, upper 16 of AXI-M ignored; MCU 40-bit via NMU-remap apertures. FORBIDDEN: MCU direct >40-bit reach. | S1 §1.5.3 (PDF p7); S2:2176. | v1.1 added 40-bit MCU handling. | EVIDENCE_PENDING | doc-only | Trace |
| INV-SEP-ISO-01 | ALLOWED: enc/dec normal op unaffected by the other's hard/soft reset, clk change, clk gating, re-init, MCU reset, error recovery (incl NMU reset). FORBIDDEN: cross-engine disturbance on any listed event. | S1 §1.5.6 verbatim list (S1:396-402). | v1.2-NEW. | EVIDENCE_PENDING | doc-only (superset of C-03) | Trace dynamic + shared-resource |

---

## 5. Legal Temporal Sequences (allowed ordering)

### TS-1: Normal boot → operational — coverage: EVIDENCE_PENDING; maturity: doc-only, regspec-corroborated (bit map); EVID needed: Chronos ordering proof
Ordered (S2 pre-config S2:2825-2892): power ON → release `pmc_por_rst_b` → release `vcu2_npi_preset_n` → poll `MMD_PWR_SUPPLY==1` (STATUS bit25, S5) → unlock PCSR `0xF9E8D7C6` (S2:2832) → de-assert IPOR (mask bit30 then control=0, S2:2834-2835 / S5) → `ECO_REG_2[2:0]=3'b010` (EDT-1066811) → eFuse over SSC; poll `SSC_EFUSE_VALID` → release DFX iso (ISO_0/ISO_4 = bits23/27, S5) → scan-clear trig=0xf, wait 20µs, poll DONE&PASS==0xf else HALT → BISR (trig bit29, DONE bit23) → NPI-clock switch + mem-clear (trig bits17-20) poll DONE&PASS → check eFuse valid; if BOTH disabled → NO reg access (INV-EFUSE-BOTHDIS-01) → release func iso (ISO_1/3/2=bits24/26/25) → de-assert INITSTATE (bit6) → assert PCOMPLETE (bit0) → NoC driver.
- FORBIDDEN deviations enforce INV-RESET-SEQ-01, INV-RESET-RDC-01, INV-DISABLE-01.

### TS-2: eFuse enable (SM-1) — coverage: EVIDENCE_PENDING; maturity: doc-only; EVID needed: Trace FSM
ssc_efuse_en asserted → 4 in-order SEC packets → on de-assert, valid asserts → disable_ip status latched → NPI valid/error interrupts. Error recovery: SW resends (S2:2050 Note 3). Lockout recovery: por_pl_b only (INV-SSC-02).

### TS-3: Independent ENC hang recovery — coverage: EVIDENCE_PENDING (C-03); EVID needed: Trace dynamic behavior + reachability + authority
Intended (DID design text S2:2694-2696, 2741): assert `pl_vcu2_spare_in[56]` (ENC RAW RST) → resets Enc core+MCU (L1+L2) only → Dec continues (would satisfy INV-SEP-ISO-01).
**Chronology (reconciled, per Echo retraction):** interim Jira EDT-1069519 comments discussed SW-reset acceptance → later DID revision describes independent HW pins → supplied RTL contains port-level hookups for the spare-pin resets. This settles the *history/order*. It does NOT settle: (a) functional connectivity (port presence ≠ proven isolated end-to-end reset), (b) authority precedence, (c) target applicability/reachability (Table 10 "Reachability" column BLANK for [56]/[11]). Coverage: EVIDENCE_PENDING (Trace dynamic + shared-resource + authority).

### TS-4: PL Partial Reconfiguration — coverage: BLOCKED (mechanism gap A-04, off-disk); maturity: doc-only; EVID needed: Ledger/Trace
PL region under PR → PMC shuts down VCU → after PR, VCU reinitialized; S2:2141 "VCU will gracefully start working after being reinitialized." Mechanism unstated (assume full TS-1).

### TS-5: Disabled-instance interrupt handling — coverage: EVIDENCE_PENDING; maturity: doc-only
Disabled instances removed from PLM topology CDO (no pre-config) OR IMR-masked (S2:2822, 2876). Int[10] on SC/MC fail non-maskable pre-removal.

*(Backpressure, flush/drain, abort/restart, DMA-error, timeout, multi-context, boundary-frame TS: NOT in DID — live in Allegro core specs (off-disk). See §7 A-01. Routed to Ledger/Echo for retrieval — NOT permanently unsourced.)*

---

## 6. DID / Architecture Conflicts & Discrepancies

### C-01 — Document revision-binding mismatch (DID→v1.0 vs supplied v1.2). coverage: BLOCKED (on release/approval record, off-disk); disposition: document revision-binding mismatch confirmed (NOT target authority).

### C-03 — INV-RESET-ISOLATION-01: independent ENC/DEC HW reset — coverage: EVIDENCE_PENDING (dynamic/shared-resource/authority/target open; chronology reconciled)
- Arch requires (v1.2 §1.5.6 S1:396-402 + §1.6 S1:424-425).
- DID design (S2:2676-2677): original had ONE shared HW reset ("This was missed"); "Fix is to have two additional HW reset sources" via spare pins [56]/[11]. **Qualifier (Orion):** this is INTENDED design text, not proof of accepted implementation/silicon.
- Chronology (Echo, reconciled): interim-SW-discussion → later-DID-HW-description → RTL-port-presence.
- **Trace-reported structural evidence (independent audit PENDING):** Trace reports that, for the **supplied analyzed filelist closure**, `spare_in[56]`/`[11]` wire into DISJOINT ENC/DEC reset cones (BLH tie-off noncompiled collateral). This is Trace-reported structural evidence, NOT yet audited by Sentinel and NOT reviewed by Breaker/Prover; it is bounded to the supplied analyzed closure, not overall structural isolation (dynamic + shared-resource isolation, target config, and build execution remain open).
- **STILL OPEN:** (a) legal DYNAMIC behavior (async assert/release policy, in-flight AXI disposition, recovery timing — Chronos/Breaker + Allegro contract, see §7.1); (b) shared-resource isolation (interconnect/SLCR/APM cross-effects); (c) target applicability/provenance (which config/stepping ships this closure; Table 10 reachability blank; only T-note on disk = EDT-1077095 "LP corner not supported for T50", S2:2469); (d) authority precedence.

**Trace-reported structural detail (`trace_rtl.md`, Sentinel/Breaker/Prover review pending):** `core_top.sv:1014-1015` `pl_vcu2_enc_raw_rst_n=spare_in[56]`, `pl_vcu2_dec_raw_rst_n=spare_in[11]`; downstream mask (`:1358`) zeros bits 56/11 so each has exactly one consumer. `vcu2_reset.sv` (pure combinational, 0 flops): `vcu2_enc_rstn = common & ssc_fuse_valid_enc & pl_vcu2_enc_raw_rst_n` (:67-77), `vcu2_dec_rstn = common & ssc_fuse_valid_dec & pl_vcu2_dec_raw_rst_n` (:81-91); single-load sinks (`and2_reset_6`→enc, `and2_reset_7`→dec) reported as DISJOINT cones, active-low. Trace reports `spare_in[56]=0,[11]=1` resets ENC, leaves DEC+interconnect running (symmetric). This narrows (not closes) C-03 for the supplied analyzed closure; Echo's interim SW-only hypothesis is inconsistent with this RTL, per Trace.

**ATLAS legality rulings on Trace's open questions:**
- **Q1 (async reset-deassert / RDC at Allegro IP boundary) — coverage: BLOCKED (Allegro IP contract off-disk); pre-candidate (Trace O1).** `vcu2_reset.sv` combinational → enc/dec rstn deassert ASYNC to all clocks; raw nets enter hard-IP core-reset pins directly (`int_wrap.sv:2274` enc; `:2031/2045/2048/2060` dec). Arch (S1 §1.5.6/§1.6) requires isolation but is SILENT on reset-deassert synchronization at the IP boundary. Deciding contract = Allegro AL-E200-E/AL-D300-S IP spec (OFF-DISK). Outcomes: (a) IP self-synchronizes reset removal → external sync redundant → O1 closed; (b) IP requires boundary-synced reset → async-deassert on functional PL pin = real RDC exposure → O1 live. Trace's DEC comment (`int_wrap.sv:2453-2454` "sync only for APMs — DEC_TOP has its own reset synchronizer") LEADS toward (a) for DEC. Coverage: BLOCKED (controlling Allegro contract unavailable prevents adjudication; route Ledger); pre-candidate is a type, not a state.
- **Q2 (spare-pin [56]/[11] as FUNCTIONAL reset) — coverage: BLOCKED (interface/pin-authorization spec off-disk).** Architecture SANCTION NOT FOUND in searched source: Arch v1.2 has 0 hits for `spare|repurpose|PL-pin-reset`; it does not enumerate an authorized spare-pin map. The DID SUPPLIES the repurpose description (S2:2677 "Spare input pins from PL will be repurposed and combined with the reset to encoder and decoder blocks") + PD timing comment (`core_top.sv:1013`). A separate documented repurpose window spares[8:0] exists (`int_wrap.sv:435`; `:1107` uses [8]); [56]/[11] fall outside THAT window, but that window is NOT established as exhaustive for the same interface/config, so this is NOT proof the pins are unauthorized. DISPOSITION: architecture sanction not found in searched source; DID supplies the repurpose description; whether [56]/[11] are production-functional-reachable vs test/debug is unresolved (matches blank Table 10 Reachability). No "unauthorized mechanism" inference drawn.
- **O3 (2 ENC cores share single `vcu2_enc_rstn`) — coverage: EVIDENCE_PENDING.** No per-encoder-core-reset invariant found in searched architecture corpus: isolation granularity stated in S1 §1.5.6/§1.6 is ENCODER-vs-DECODER; all "each core" arch refs (S1:241/281/294/540) concern encoding capacity, not reset; ULL 2-stream mode (S1 §1.11.4.1) states no per-core reset requirement. Absence of a per-core requirement is NOT affirmative permission — governing authority/target for per-core reset remains EVIDENCE_PENDING. Observation retained: a single ULL-stream hang forcing both enc cores to reset would be a degradation; no stated invariant addresses it.

### C-04 — EDT-1072959 rev-history mislabel — coverage: BLOCKED (Jira linkage off-disk); maturity: open discrepancy, NOT certified bug
- DID rev-history (S2:47-48) labels 1.5.1: "EDT-1072959 fix: Updated Abus Switch DPSTx2 section."
- Orion/Echo: EDT-1072959 is power-aware Reify assertion work, NOT ABUS/DPSTx2.
- Classification (Orion): OPEN source/documentation discrepancy; not a certified doc bug until original table/Jira linkage evidenced. BLOCKED on Jira.

---

## 7. Undocumented Assumptions & Gaps (routing noted)

- **A-01** [coverage: BLOCKED] Core datapath state machines (start/completion, backpressure, flush/drain, abort, DMA-error, timeout, boundary frames) NOT in DID (cores are [ALG] black boxes above slice level). Live in `encoder_specification.pdf` / `decoder_specification.pdf`. → **ROUTED to Ledger/Echo** (URLs below), NOT permanently unsourced.
- **A-02** [coverage: EVIDENCE_PENDING] Trust zone = "TBD" (S2:2203). AxPROT/SMID in S1 §1.32-1.33 only.
- **A-03** [coverage: BLOCKED] Multi-instance 8Kp30 inter-instance PL interface (S1 §1.5.2.2 PDF p8) — mechanism absent in DID.
- **A-04** [coverage: BLOCKED] PR reinit "graceful start" (TS-4) — no mechanism.
- **A-05** [coverage: EVIDENCE_PENDING] DID numeric inconsistencies: enc reg window "1M 64K" ambiguous (S2:2178); remap `0xE81000000` = 9 hex digits > 48-bit tile base (S2:702). → cross-check against S5 encoder/decoder regspec address windows (pending).
- **A-06** [coverage: EVIDENCE_PENDING] eFuse polarity "[need to check with Renuka]" (S2:1987) — unconfirmed in DID (RTL has disable bits per S5; polarity still doc-unconfirmed).
- **A-07** [coverage: EVIDENCE_PENDING] VDU→VCU naming bleed throughout (many "VDU"/"VCDU" residuals; reset Open-Issue #7 "PendingClosed"). Provenance caveat: DID partly derived from VDU DID.
- **A-08** [coverage: EVIDENCE_PENDING] Mem-clear wait "[Need value] µs" (S2:2867) — TBD.
- **A-09 (EOF-SYNC-01)** — coverage: **EVIDENCE_PENDING** (materially WEAKENED — no failing on-disk config demonstrated). Candidate authority = **Jira EDT-1094051** (via Echo), NOT architecture.
  - **CURRENT-CONFIG EXPOSURE — NOT demonstrated on disk (Prover E10, coherent-wrapper finding):** Prover reports ALL on-disk wrappers are coherent: LIVE EOF is generated ONLY in the narrowing LLM config (`!ONE_INSTANCE && LLP_MODULE`, which regenerates the pulse via `xpm_cdc_pulse`), while the enc/dec pass-through wrapper configs TIE EOF LOW. Therefore NO on-disk supported config presents a live multi-cycle EOF into the level-sensitive consumer — no failing on-disk configuration is demonstrated. The int_wrap.sv level-sensitive consumer text does NOT, by itself, prove a reachable multi-cycle input; on disk it is either pulse-narrowed (LLM) or tied low (pass-through).
  - **SEPARATE — historical/documentation contract gap (retained):** the EOF pulse contract is still "not found in searched text-extract/regspec corpus" (0 hits S1/S2/SLCR/NPI); Jira EDT-1094051 is the only candidate authority. This documentation gap is distinct from current-config exposure.
  - **WEAKENED for user-reachable non-LLP exposure (Echo/E12 — mapping INFERRED, not proven):** Confluence TDL page 753167822 v23 and the E12 IP Wizard document low-latency PORT GATING — the `sync_eol`/`sync_eof` "sync IP" ports are enabled only when low-latency=true AND encoder enabled. HOWEVER, the IP Wizard does NOT name `LLP_MODULE` and does NOT prove that user LOW_LATENCY_MODE maps to the specific narrowing branch (`!ONE_INSTANCE && LLP_MODULE`). The LOW_LATENCY_MODE→LLP_MODULE/module mapping is INFERRED, pending packaging metadata. So "no user-reachable non-narrowed live-EOF config" is PLAUSIBLE but NOT proven — it rests on an unproven param mapping. A simulation-harness XMR can exercise a non-narrowed EOF path, but that is NOT product evidence. (Attribution: Echo/Prover via Confluence TDL 753167822 v23 + E12; no MCP/Confluence access here — cited as peer-reported; mapping explicitly INFERRED.)
  - **RECLASSIFIED toward historical PL/netlist issue (Ledger Rev B) — weakened, non-categorical:** retrieved history reports a PL/netlist/software remedy; **T50** narrow presilicon reported fixed; **T40** fixed relayed by implication (not directly confirmed); **T3** directly reported OPEN; CL/netlist unopened. The absence of the active edge-fix in VCU RTL is **non-probative** (NOT categorically "expected") — it is consistent with, but does not prove, a PL-netlist-owned remedy. Do not state "T40/T50 fixed" categorically or "PL FBW owns remedy" as proven. ⇒ EOF-SYNC-01 is treated as a historical PL/netlist item with target-applicability open, not a confirmed VCU-RTL defect. D1 (documentation contract gap) retained as a doc/history note only.
  - Coverage: BLOCKED (target applicability — T3 open, CL/netlist unopened — off-disk). Attributions (Prover E10/E11, Echo Confluence TDL 753167822 v23, Ledger Rev B) are peer-reported; I have no MCP access to verify Confluence/Jira/netlist independently. Revision-marked DID citations pending reconfirmation against Ledger tracked-change-aware extract.
  - **High-level docs SILENT (unchanged):** EOF pulse contract **not found in searched text-extract/regspec corpus** — 0 hits for `eof|end.?of.?frame|frame.?done|pulse` (grep -niE) in S1 extract, S2 extract, `vcu2_0_slcr_regs.h`, `vcu2_npi_regs_defines.vh` (excludes embedded figures + off-disk specs). Arch v1.2 and DID 1.5.1 give no EOF pulse-width/clock-domain/edge-vs-level/loss-duplication/consumer contract. Nearest: GATED_CLOCK "command start and completion events" (S2:557/657); PL `vcu2_enc_pintbus[31:0]`/`pintreq` (S2:2242-2247).
  - **Jira-source contract (attribution: Echo, EDT-1094051 — NOT independently verified by me; no MCP/Jira access):** Echo reports EDT-1094051 states an EXPLICIT `sync_eof` contract — one encoder-clock synchronization, named source/destination clocks, an intended EDGE-REGENERATION fix, and a presilicon symptom. This is a Jira-level candidate authority; it does NOT elevate to architecture authority (high-level docs remain silent). Target applicability + active-behavior remain pending Trace/history.
  - **RTL LEAD (Trace/Prover-reported, EVIDENCE_PENDING — NOT certified proof):** signal text `vcu2_enc_sync_eof[1:0]` at `vcu2_int_wrap.sv:214` (encoder-only, 2-bit) — consistent with Echo's "one-encoder-clock" contract. In `int_wrap.sv` the EDT-1094051 edge-regeneration generator text appears COMMENTED OUT (`:737-740` decl, `:4980-4994` logic); active consumer uses level-synchronized `sync_with_clk` (`:2229`), pulse `sync_re` commented at `:2228`; sibling `sync_eol` (EDT-1059312) uses active pulse `sync_re` (`:2226`). Trace-reported (Sentinel/Breaker review pending) static active-cone behavior for the supplied analyzed source closure: filelist-selected level-sync feeds a level-sensitive consumer with no downstream edge-detect; a second high cycle WOULD cause an extra cptin increment and valid suppression, APB-observable — but this is a CONDITIONAL consequence IF a multi-cycle-high EOF reaches the consumer. Per Prover E10 (below), no on-disk supported config actually delivers such an input (live EOF is pulse-narrowed in LLM, tied low in pass-through), so the level consumer alone does NOT prove a reachable multi-cycle input. **CONFIG-DEPENDENT (Prover-reported):** an UPSTREAM PL wrapper (`vcu2_vivado_LLM_wrap`/`vcu2_vivado_dec_wrap/rtl/vcu2_v3_0_rfs.v`) narrows EOF via `xpm_cdc_pulse` ONLY in the `!ONE_INSTANCE && LLP_MODULE` branch (`vcu2_v3_0_rfs.v:679, 830, 1373-1392, 1748-1763`), with pass-through in other branches. So EOF pulse-vs-level behavior is CONFIGURATION-DEPENDENT; do NOT state multi-cycle behavior universally. **Config params observed in supplied RTL:** `design_1_vcu2_0_0.v:890-891` sets `.ONE_INSTANCE(0), .LLP_MODULE(1)` (i.e. the pulse-narrowing branch is selected in THIS instantiation); default params also `ONE_INSTANCE=0, LLP_MODULE=1` (`vcu2_v3_0_rfs.v:86-87`). WHETHER these vivado/PL-wrapper files are in the compiled VCU hard-block cone, and which config ships to the target, are Trace/target determinations — NOT established here. (A `rtl_11may_bkp/` backup-named copy of `vcu2_v3_0_rfs.v` also contains `xpm_cdc_pulse`; its active/excluded status in the closure is UNVERIFIED — not assumed non-active.) Do NOT infer revert intent, target applicability, or system ownership. Jira EDT-1094051 remains a SEPARATE candidate authority; high-level corpus silence remains bounded. Off-disk Allegro consumer contract routed to Ledger. **Breaker handoff HELD** per Orion; Trace/Prover-pending only.

- **A-10 (EDT-1095022 — 12-bit reconstructed-buffer pitch)** — disposition: **DOCUMENTATION_BUG** (Sentinel-confirmed historical), scoped strictly to the MISSING 12-bit pitch RULE; **no silicon/RTL issue alleged or reproduced**. Sentinel confirms the classification. EXPLICITLY OUTSIDE this closure (still open, NOT part of the documentation-bug finding): (a) implementation correctness of the pitch fix, and (b) target applicability — both UNPROVEN; (c) Allegro ticket 6412 and CL 62082349 remain author-attributed and UNOPENED (I have no MCP/vendor access). So the documentation gap is closed as a documentation bug; HARDWARE CORRECTNESS is deliberately kept outside closure. On-disk basis: Arch §1.35 states format/packing rules but defers exact tile/pitch byte-arithmetic off-disk (S1:874); DID has no frame-buffer section. No candidate; no RTL effort.
  - What the on-disk ARCH TEXT literally supports (verbatim only, NO derived equations): S1:869-870 "10 and 12-bit pixel color components are contained in 16-bit words" ⇒ 2 bytes per COMPONENT SAMPLE **for 10/12-bit modes only** (says nothing about 8-bit packing). S1:855-856 same for encoder raster. S1:843-847 three formats; S1:846 tile dims 64x4/32x4; S1:884-885 "4:2:0 and 4:2:2 formats use semi-planar buffers (one buffer for Y, one buffer for interleaved Cb/Cr), while 4:4:4 format uses planar buffers (3 separate buffers for Y, Cb, Cr)".
  - **NOT establishable from the corpus (labeled HYPOTHESES — do NOT treat as arch fact):** total raster pitch = `width×2` (requires alignment/row-padding rules NOT on disk); 8-bit packing behavior (arch text names only 10/12-bit as 16-bit-word-contained; 8-bit is unstated); 4:2:0/4:2:2 chroma subsampling geometry and interleaved Cb/Cr byte-pitch equations (the plane LAYOUT is cited S1:884-885, but the subsampling ratios and interleave pitch arithmetic are NOT in S1/S2 — must not be imported from general format knowledge). All pitch/geometry equations are OFF-DISK in the buffer-format doc (S1:874 "Details of the tiled format and other buffer formats are available in a separate document").
  - Disposition: documentation-only, no architecture/hardware risk; no candidate; no further pitch work.

### 7.1 Off-disk references routed to Ledger/Echo (exact metadata for authoritative retrieval)
- Encoder spec: SharePoint `…/Modules/VCU/Encoder Specs/encoder_specification.pdf` (S2:501).
- Decoder spec: SharePoint `…/Modules/VCU/Decoder Specs/decoder_specification.pdf` (S2:504).
- XRDB register DB: `http://zynq_design:8070/` and `#mod___VCU2_SLCR` (S2:717, S2:2199).
- Jira: EDT-1069519 (reset), EDT-1072959 (Reify), EDT-1066811 (clk switch), EDT-1056613 (SLVERR/FIXED), EDT-1059821 (memclear), EDT-1077095 (T50 LP), EDT-1069146 (PL int).
- Interconnect intent: `VCU_interconnect_intent.xlsx` (S2:704). Address map: `VCU2_Address_Map.xlsx` (S2:2143). Memory manifest: `VCU2_Memory_Manifest1.xlsx` (S2:784).

---

## 8. High-Risk Flows — architecture-predicted risk; ALL NONTERMINAL

> Every item is an ARCHITECTURE-PREDICTED risk (impact statements are predictions, NOT confirmed defects) pending Trace (RTL behavior), legal-reachability, history/waiver (Ledger/Echo), and verification (Prover/Breaker). Each carries exactly one coverage state.

1. **Independent ENC/DEC reset (C-03)** — coverage: EVIDENCE_PENDING. Architecture-predicted availability/safety risk IF isolation not realized dynamically. EVID needed: Trace dynamic behavior + shared-resource + reachability + authority. NONTERMINAL.
2. **SSC eFuse lockout (INV-SSC-02)** — coverage: EVIDENCE_PENDING. Architecture-predicted: mis-sequenced eFuse on PL power event leaves VCU disabled until full por_pl_b. EVID needed: Trace FSM + Chronos. NONTERMINAL.
3. **POR/pre-config register ordering (TS-1, INV-RESET-RDC-01)** — coverage: EVIDENCE_PENDING. Architecture-predicted: PLM CDO mis-order → invalid reads / SLVERR / stuck-disabled. EVID needed: Chronos ordering, Breaker. NONTERMINAL.
4. **Both-disabled reg-access lockout (INV-EFUSE-BOTHDIS-01)** — coverage: EVIDENCE_PENDING. Architecture-predicted: driver access hangs/errors when both disabled. EVID needed: Trace reset-fanout. NONTERMINAL.
5. **AXI FIXED→APB SLVERR (INV-APB-01)** — coverage: EVIDENCE_PENDING. Architecture-predicted: master issuing FIXED to enc/dec APB gets SLVERR by design. EVID needed: Breaker + Trace. NONTERMINAL.

---

## 9. Responses to Peer Challenges (log)
- Orion SCOPE/ARCH/RESPONSE — answered; 4 applicability axes separated; C-01 reclassified to document-binding-only. Accepted.
- Sentinel SENT-006 — live-tip semantics corrected. Closed.
- Sentinel ACK — family mapping ≠ target-binding. Incorporated (IDENT-01).
- Orion/Echo EDT-1069519/1072959 — C-03/C-04; "intended not proven" qualifier; EDT-1072959 = open doc discrepancy.
- Echo retraction — TS-3/C-03 chronology reconciled; functional/authority/target retained open.
- **Sentinel SENT-008 / SENT-010 — OPEN / pending verification (NOT accepted).** Corrections applied this cycle: canonical SENT-010 coverage-state vocabulary (NOT_STARTED/IN_PROGRESS/EVIDENCE_PENDING/BLOCKED/INVESTIGATED_NO_ISSUE/CANDIDATE_FOUND/PROVER_REVIEW_PENDING/COMPLETE), with SPEC_ARCH_CONFLICT as a disposition (not a state) and maturity labels on a separate axis; per-item ALLOWED/FORBIDDEN; reproducible citations with FULL SHA-256 (source+extract) + extraction tool/version + search terms/counts (extracts in canonical root); §8 each carries one coverage state + NONTERMINAL; TS-3 chronology reworded; C-01 document-binding-only; C-03 reworded to Trace-reported / EVIDENCE_PENDING (no SATISFIED/accepted); O3 reworded to "no per-core invariant found in searched corpus"; Q2 reworded to "architecture sanction not found; DID supplies repurpose description" (no unauthorized inference); §10 totals recomputed from allowed states only; S6 provenance uses actual hashes (source SHA-256; workspace-config MD5 explicitly labeled). Awaiting Sentinel re-verification.
- Outbound: Trace (reset RTL discriminator), Chronos (reset temporal facets + gaps). Both PENDING.

---

## 10. State Totals (this revision)
Recomputed from canonical SENT-010 coverage states ONLY (maturity + disposition labels excluded from counts). No item is COMPLETE. SPEC_ARCH_CONFLICT is a disposition, not a state.
- **Invariants (11):** EVIDENCE_PENDING = 11 (all). BLOCKED = 0. (INV-RESET-ISOLATION-01 & INV-SEP-ISO-01 carry Trace-reported-structural maturity but coverage remains EVIDENCE_PENDING pending dynamic/shared-resource/authority.)
- **Conflicts/discrepancies (4):** BLOCKED = 3 (C-01 [disposition SPEC_ARCH_CONFLICT], C-02, C-04). EVIDENCE_PENDING = 1 (C-03).
- **Temporal sequences (5):** EVIDENCE_PENDING = 4 (TS-1, TS-2, TS-3, TS-5). BLOCKED = 1 (TS-4, off-disk mechanism).
- **Reset legality sub-items:** Q1 EVIDENCE_PENDING; Q2 BLOCKED (off-disk pin-authorization); O3 EVIDENCE_PENDING.
- **Gaps A-01..A-09:** A-09 EVIDENCE_PENDING; A-01/A-03/A-04 BLOCKED (off-disk); A-02/A-05/A-06/A-07/A-08 EVIDENCE_PENDING.
- **High-risk flows (5):** all EVIDENCE_PENDING, all NONTERMINAL.
- Gaps A-01..A-09 (9): BLOCKED 3 (A-01, A-03, A-04), EVIDENCE_PENDING 6 (A-02, A-05, A-06, A-07, A-08, A-09); off-disk refs routed to Ledger/Echo.
- BLOCKED: MCP (Jira/XRDB/Confluence/RAG), exact P4 changelist, user-target SKU/stepping, off-disk Allegro core specs.

## 11. Sources Searched
- S1 full (996-line extract, all 22 pp). S2 targeted: block desc S2:520-860, NPI/SSC/interrupt S2:1400-2260, reset/POR/pre-config/startup S2:2671-2920, clocking S2:2259-2340, address/trust-zone S2:2140-2260, Open Issues S2:55-120.
- S5 RTL regspecs: `vcu2_0_slcr_regs.h` (base/version), `vcu2_npi_regs_defines.vh` (PCSR/PSR bit map). Decoder/encoder vendor regspecs enumerated, content cross-check pending (A-05).
- Workspace config, RTL top listing.
- NOT searched / unavailable: Jira, XRDB (zynq_design:8070), SharePoint specs, embedded DID figures, encoder/decoder core regspec contents.

*End Cycle-1 revised model. Next: S5 encoder/decoder regspec address-window cross-check (A-05), remaining DID reads (§3.5 timing, DFX detail, interrupt-tree figures).*
