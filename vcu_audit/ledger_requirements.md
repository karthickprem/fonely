# LEDGER — VCU2 Requirements Forensics

**Agent:** did-ledger (LEDGER) · **Role:** requirements authority / expected-behavior determination
**Mode:** read-only. No requirement, Jira, Confluence, GitHub or source file was modified.
**Status:** Phase 1 (primary-source normative extraction) complete for both supplied primary documents. Release applicability **not** asserted — Orion has not frozen scope (no T20/T40/T50 or stepping).
**Last updated:** 2026-08-12

---

## 0. Reading rules for this document

- **[VERBATIM]** = characters copied from the original artifact.
- **[INTERPRETATION]** = my inference. Never treat as source text.
- **[SPEC_AMBIGUITY]** = the documents are silent or self-contradictory. No expected behavior is invented.
- Docs RAG was used for *discovery only*. Every finding below was re-verified against the original file on disk or the live Confluence page. Anything not so verified is labelled **[UNVERIFIED-RELAY]**.
- DOCX has **no stable pagination**. DID citations are therefore given as *heading path + table index + line number in a reproducible extraction* (extraction method in §1.3), never as page numbers.

---

## 1. Artifact and Revision Manifest

### 1.1 Primary artifacts (opened directly by me, byte-level)

| # | Artifact | Path | md5 | Size (B) | mtime | Internal revision |
|---|---|---|---|---|---|---|
| A1 | Telluride DID — VCU | `/everest/apex_pvs_nobkup/karthick/vcu_analysis/Telluride_DID_1.5_VCU.docx` | `25f514d0d2ea70da9f85183e85e75f0e` | 7,912,786 | 2026-07-07 16:57 | **1.5.1** (see §1.2) |
| A2 | Everest MMD VCU Architecture Specs | `/everest/apex_pvs_nobkup/karthick/vcu_analysis/Everest_MMD_VCU_Arch_Specs_1.2 (1).pdf` | `1b8f85119435ee3385d9c8c0b18559dd` | 690,707 | — | **Version 1.2**, 22 pages, PDF CreationDate 2024-06-22, Author metadata "Xilinx", © 2022-2024 |

### 1.2 **FINDING L-M01 — DID filename revision ≠ DID internal revision**

Filename says `1.5`. The document's own revision-history table says `1.5.1`, twice, with two different dates and two different content deltas.

> **⚠ PARTIALLY RETRACTED — see §12 (Rev B).** The "two empty rows" claim below is **withdrawn**: rows r2/r3 are *tracked insertions*, not blanks. The filename-vs-internal-revision finding and the duplicate-`1.5.1` finding **stand**. Corrected dump in §12.2.

Location: **Table index 0** (first table in the document body), under **[[Heading 1]] "VCU DID Revision History"**.
Extraction line range: `did.txt:5–16`. Raw cell dump (`python-docx`, `document.tables[0]`) — **this reader is now known to be defective; see §12.1**:

```
r0: Version | Date | Editor | Version Comments
r1: 0.5.0  | 06/20/2022 | Anurag Agrawal | Initial draft
r2: (all cells empty)   <-- RETRACTED: = inserted row "0.9.0 | 10/27/2022 | Anurag Agrawal"
r3: (all cells empty)   <-- RETRACTED: = inserted row "1.0.0 | 11/15/2022 | Anurag Agrawal"
r4: 1.1.0  | 02/10/2022 | Anurag Agrawal | BPD POR implementation
r5: 1.2.0  | 08/20/2023 | Anurag Agrawal | Sysmon Implementation
r6: 1.5.0  | 09/10/2023 | Anurag Agrawal | Interrupt Tree Diagram / DFX Arch Updates /
                                            NPI Interrupt – Scan clear & mem clear updates /
                                            Partial reconfiguration proposal / Area Estimation Updates
r7: 1.5.1  | 07/15/2024 | Abhay Galagali | EDT-1069519 fix: Updated reset section.
                                            EDT-1072959 fix: Updated Abus Switch DPSTx2 section
r8: 1.5.1  | 31/10/2025 | Abhay Galagali | Cleaned-up TBDs.
```

Document-control anomalies, all **[VERBATIM]** from the cells above:
- **Duplicate version number.** Rows r7 and r8 both carry `1.5.1`. The 2025-10-31 edit ("Cleaned-up TBDs.") was published **without a version increment**. The content in hand is therefore *newer than any version label in the document*.
- **Mixed date formats.** r1/r4/r5/r6/r7 are `mm/dd/yyyy`; r8 is `dd/mm/yyyy` (`31/10/2025` cannot be mm/dd).
- **Out-of-order history.** r4 `1.1.0` is dated `02/10/2022`, i.e. *four months before* r1 `0.5.0 — Initial draft` dated `06/20/2022`.
- **Two empty rows** (r2, r3) between the initial draft and 1.1.0 — revision entries either deleted or never filled.

**[INTERPRETATION]** The DID has no reliable version identity. Any peer citing "DID 1.5" must instead pin the md5 in §1.1. I will cite this artifact as **`DID@25f514d0` (labelled 1.5.1, content dated 2025-10-31)**.

### 1.3 Extraction provenance (reproducible)

- Arch spec → `/tmp/ledger_extract/arch_spec.txt`, 996 lines:
  `pdftotext -layout "…/Everest_MMD_VCU_Arch_Specs_1.2 (1).pdf" arch_spec.txt`
  Printed page numbers survive in the text, so arch-spec citations below are **real PDF page numbers**, cross-checked against the document's own Table of Contents (`arch_spec.txt:35–79`).
- DID → `/tmp/ledger_extract/did.txt`, 1238 lines: `python-docx`, iterating `document.element.body.iterchildren()` so tables are preserved inline as `[[TABLE]] … [[/TABLE]]` pipe-delimited rows and headings tagged `[[Heading N]]`. Hyperlink targets read separately from `word/_rels/document.xml.rels`.
- These are **my working copies**, non-canonical. Every quotation below was taken from them and each load-bearing one re-checked against the original container (`.docx` XML / PDF page).

### 1.4 Requirement hierarchy as *stated by the documents*

```
Everest (Telluride) MMD VCU Architecture Requirements     ← DID declares this as its parent
        │
        └── Telluride DID — VCU (implementation spec)
                │
                └── VCU/VDU Verification Spec  ← does not exist for VCU (see §2.3)
```

**[VERBATIM]** DID §Introduction → Scope (`did.txt:155`):
> "This document provides the detailed engineering specification of Telluride Video Codec Unit for a target process technology of 7nm. The detailed specifications are based on the Everest (Telluride) MMD VCU architecture requirements."

**[VERBATIM]** DID §Introduction → Functional Requirements, first line (`did.txt:49`):
> "Reference: Everest MMD VCU Arch Specs v1.0"

**[VERBATIM]** DID §Introduction → Starting References, **Table index 2**, row "Architecture Specifications" (`did.txt:135–147`):
> `Architecture Specifications | VCU Arch Specs`

The anchor text `VCU Arch Specs` is `rId13`. Its target, read from `word/_rels/document.xml.rels` and URL-decoded:

```
https://xilinx.sharepoint.com/sites/eng/fdst/everest/arch/Shared Documents/Forms/AllItems.aspx
  ?id=/sites/eng/fdst/everest/arch/Shared Documents/Arch Specs (released)/Telluride/MMD/
      Everest_MMD_VCU_Arch_Specs_1.0.pdf
  &parent=/sites/eng/fdst/everest/arch/Shared Documents/Arch Specs (released)/Telluride/MMD
```

**FINDING L-M02 — the DID pins architecture v1.0 in two independent places.** The `id=` component names the **file** `Everest_MMD_VCU_Arch_Specs_1.0.pdf` inside the **`Arch Specs (released)`** library; the `parent=` component is the version-agnostic MMD folder. This **independently confirms Prover's claim** and upgrades it from "reference-table wording" to a machine-readable link target. The v1.0 pin is therefore not a typo in prose — it is the document's actual hyperlink.

**FINDING L-M03 — the supplied artifact is v1.2; the DID's normative binding is v1.0.** Two full minor revisions of the parent specification are unaccounted for in the DID.

**Resolution status of "was v1.2 formally released?" — UNRESOLVED, and I will not resolve it from RAG.**
Evidence I *can* stand behind:
- The DID's own link proves **v1.0 was in `Arch Specs (released)`** as of the DID edit that inserted it.
- Confluence XSIC/792798238 v87 (§2.2) has an **Architecture Specs** section whose header links to `.../Arch Specs (released)`, and whose **VCU row does not point at any VCU architecture spec at all** — it points at an evaluation report.
- The v1.2 PDF in hand carries `Version 1.2` on its cover (`arch_spec.txt:12`, PDF page 1) and CreationDate 2024-06-22, but a version string on a cover page is **not** a release record.
- **What would close this:** a directory listing of `https://xilinx.sharepoint.com/sites/eng/fdst/everest/arch/Shared Documents/Arch Specs (released)/Telluride/MMD/`. I have no SharePoint read path from this session. **Requesting it from Orion.** Until then: `SPEC_AMBIGUITY L-A01`.

---

## 2. Approval Status — authoritative evidence

**Source (opened live, not relayed):** Confluence Cloud, space **XSIC** ("Silicon"), page id **792798238**, title *"Telluride - Arch Spec, DID, Vspec- Rev Approval Tracking"*, **version 87**, URL `https://amd.atlassian.net/wiki/spaces/XSIC/pages/792798238`.
Page-level instruction, **[VERBATIM]**, first line of the page body:
> "**(Please check mark the box for approval)**"

**[INTERPRETATION]** Therefore an unchecked box = approval not granted. Absence is meaningful on this page, by the page's own instruction.

### 2.1 DID approval row — VCU

Section: **"Design Implementation Specs - DID"**. Columns, in order:
`Block/ Each SharePoint Folder Links | Link to Rev1.0 | DID - Functional Safety (FuSa) | Verif | SW | SSW | GOQ | DFx | SYSMON | CFG | Security | Arch | Author`

VCU row, cell by cell **[VERBATIM]**:

| Column | Cell content |
|---|---|
| Block | `VCU` → `…/Design Implementation Documents (DID)/VCU` |
| Link to Rev1.0 | `VCU2_DID.aspx` → `…/Design Implementation Documents (DID)/VCU/VCU2_DID.aspx` |
| (tracker macro) | Xilinx Engineering JIRA issue **PSEV-23069** |
| FuSa | `3163 incomplete Reviewed` · `3164 incomplete Feedback Provided` |
| Verif | `3165 incomplete` · `3166 incomplete` |
| SW | `3167 incomplete` · `3168 incomplete` |
| SSW | `3169 incomplete` · `3170 incomplete` |
| GOQ | `3171 incomplete` · `3172 incomplete` |
| **DFx** | `3173 **complete** Reviewed` · `3174 **complete** Feedback Provided` |
| SYSMON | `3175 incomplete` · `3176 incomplete` |
| CFG | `3177 incomplete` · `3178 incomplete` |
| Security | `3179 incomplete` · `3180 incomplete` |
| Author | `and` |

**FINDING L-M04 — the VCU DID carries exactly one discipline sign-off (DFx). Ten of eleven review columns are `incomplete`, including Verif, Arch, SW, SSW, Security and FuSa.**
**FINDING L-M05 — the approval row's artifact link is `VCU2_DID.aspx`**, a SharePoint *shortcut* object, not a versioned document. The tracker therefore cannot establish *which revision* was reviewed. The DID in §1.1 cannot be tied to this row by revision.
**FINDING L-M06 — the `Author` cell for VCU contains the literal string `and`.** Data-quality defect; no accountable owner recorded on the approval row.

### 2.2 Architecture-spec approval row — VCU

Section header: **"Architecture Specs- [Architecture - Arch Specs (released) - All Documents]"**.
Columns: `Block | Link to Rev1.0 | Author | Target 1.0 Date | Design | Verif | SW | SSW | GOQ | SYSMON | CFG | Security | Functional Safety`

VCU row, **[VERBATIM]**:

| Column | Cell content |
|---|---|
| Block | `VCU` |
| Link to Rev1.0 | `VCU2_Eval_Report_Summary_rev2.pdf` → `…/Arch Specs (released)/Telluride/**VCU2_eval**/VCU2_Eval_Report_Summary_rev2.pdf` |
| Author | *(empty)* |
| Target 1.0 Date | `9/30/2022` |
| Design | `1641 incomplete` · `2185 incomplete` |
| Verif | `1642 incomplete` · `2186 incomplete` |
| SW | `1643 incomplete` · `2187 incomplete` |
| SSW | `2026 incomplete` · `2188 incomplete` |
| GOQ | `1645 incomplete` · `2189 incomplete` |
| **SYSMON** | `1857 **complete** Reviewed` · `2190 incomplete Feedback Provided` |
| CFG | `1858 incomplete` · `2191 incomplete` |
| Security | `1859 incomplete` · `2192 incomplete` |
| Functional Safety | `1650 incomplete` · `2193 incomplete` |

**FINDING L-M07 — the VCU architecture-spec approval row does not reference a VCU architecture specification.** It links `VCU2_Eval_Report_Summary_rev2.pdf`, in a sibling folder `Telluride/VCU2_eval/`, not `Telluride/MMD/` where `Everest_MMD_VCU_Arch_Specs_1.0.pdf` lives (per the DID's own hyperlink, §1.4). An *evaluation report* is not an architecture specification.
**FINDING L-M08 — no VCU architecture specification of any revision (1.0, 1.1 or 1.2) is tracked for approval on this page.** Consequently there is **no approval record for v1.2 on the authoritative tracker**, which is the strongest evidence currently available bearing on `SPEC_AMBIGUITY L-A01` — necessary-but-not-sufficient: the tracker's VCU row is demonstrably wrong about *which artifact* it tracks, so its silence about v1.2 is weak evidence.
**Contrast within the same table:** ISP-MMD is tracked at `Everest_MMD_ISP_Arch_Specs_V1.1.pdf` and NoC at `Arch_Spec_NoC_Telluride_v1.2_change_bar.pdf` — sibling MMD blocks *are* tracked at their true specs and at revisions >1.0, so the VCU row is anomalous, not the table's convention.

### 2.3 Verification-spec approval row — VCU

Section: **"Verification Specs-"**. VCU row, **[VERBATIM]**:

| Column | Cell content |
|---|---|
| Block | `VCU/VDU` |
| Link to Rev1.0 | `VDU Verification Spec.docx` → `…/Verification Specification Documents (VSpec)/**VDU**/VDU Verification Spec.docx` |
| Author | *(empty)* |
| Target 1.0 Date | `8/25/2020` |
| Arch / Design / SW / SSW / GOQ / DFx / SYSMON / CFG / Security / FuSa | **all `incomplete`** (ids 1807/2504 … 1816/2513) |

**FINDING L-M09 — there is no VCU verification specification.** The row is a **VDU** document, dated to a 2020 target, with **zero** completed reviews across all ten disciplines. VCU and VDU are distinct blocks (the RTL tree carries `rtl/vcu2_v1_7t` and `rtl/vdu_7t_n1` separately).

### 2.4 Consolidated authority verdict

**FINDING L-M10 — no artifact in this investigation is fully approved.** The requirement hierarchy is:
1. an architecture specification whose *approval row points at the wrong file* and whose supplied revision (1.2) is two minor revisions ahead of the DID's binding (1.0), with no release record obtainable from this session;
2. a DID with one of eleven sign-offs, an unversioned link, no recorded author, a duplicated version number, and content published after its last version increment;
3. no verification specification at all.

**[INTERPRETATION] — precedence guidance for peers.** Because approval is absent nearly everywhere, "which document wins" cannot be settled by approval status. The only *document-stated* precedence is the DID's own subordination clause (§1.4): the DID derives from the architecture requirements. Therefore, **where the DID and the architecture spec conflict, the architecture spec states the requirement and the DID states (at most) an implementation decision — and an implementation decision that narrows a `shall` is a deviation requiring a waiver, not a superseding requirement.** No waiver register has been located (§6). Peers must not treat DID silence as requirement removal.

---

## 3. Requirement Inventory — atomic records

Field order is the mandated 14-field form. `XRDB` fields are **`XRDB-UNAVAILABLE`** throughout (§5).

Applicability note applied to **every** record: the documents say **"Telluride"**, **"Everest (Telluride)"**, **"Everest MMD"**. **Neither primary document names T20, T40, T50, Telluride-small, A0 or ES1 anywhere.** Every record below is therefore tagged `Release: Telluride/Everest MMD VCU (VCU2) — document-stated only; device variant and stepping NOT stated`.

### 3.1 Architecture-spec requirements (source A2, v1.2)

---
**REQ-ARCH-001 — Encoder core count and per-core throughput**
- Source/rev: A2 `Everest_MMD_VCU_Arch_Specs` **v1.2** · §1.3 Features and Requirements Summary · **PDF p.5**
- **[VERBATIM]** "The encoder **shall** include 2 encoding cores, each independently and concurrently capable of encoding up to 4Kp30"
- Release/config: as above · Preconditions: none stated
- Required: 2 encoder cores; each independently AND concurrently capable of 4Kp30
- Prohibited: single-core encoder; cores that cannot run concurrently
- Affected interface/state: encoder core instantiation, encoder clocking
- Verification linkage: **none** (no VCU VSpec exists — L-M09)
- Ambiguity: none
- Concordance: DID §Functional Requirements **[VERBATIM]** "Dual core encoder and single core decoder" (`did.txt:50`) — **agrees**

---
**REQ-ARCH-002 — Decoder core count**
- Source/rev: A2 v1.2 · §1.3 · **PDF p.5**
- **[VERBATIM]** "The decoder **shall** include 1 decoding core"
- Required: exactly one decoding core · Prohibited: multi-core decoder
- Verification linkage: none
- **Ambiguity: `SPEC_AMBIGUITY L-A02`** — DID **Open Issue #1** is still open against this. **[VERBATIM]** DID Table index 1, row 1 (`did.txt:21`): `1 | (Status blank) | Multi core decoder | In the architecture specs, it is defined that decoder has only one core. If that is the case, multi core decoder should not be used anywhere.` The `Status` cell is **empty** while rows 3,4,5,8,9,10,11,12,13 of the same table read `Closed` — so #1 is open by the table's own convention. **The requirement itself is unambiguous; what is open is whether the implementation honours it.**

---
**REQ-ARCH-003 — Voltage operating levels**
- Source/rev: A2 v1.2 · §1.3 · **PDF p.5**
- **[VERBATIM]** "Support operation at **LP, MP and HP** voltage levels"
- Required: operation at LP **and** MP **and** HP
- **CONFLICT: L-C01** (see §4)

---
**REQ-ARCH-004 — 8K encode via dual core, single instance**
- Source/rev: A2 v1.2 · §1.3 · **PDF p.5**
- **[VERBATIM]** "One VCU encoder instance **shall** support 8Kp15 encoding using both cores, each encoding a 4Kx4K tile"
- Required: 8Kp15 encode from one instance, both cores, 4Kx4K tile each
- Affected: encoder tiling, L2 cache sizing, AXI bandwidth
- **CONFLICT: L-C02** (see §4). Related limitation, **[VERBATIM]** §1.11.4.2, **PDF p.13**: "when encoding 8Kx4K streams, the cache size is inadequate, and the same 2 mitigation choices are available" (reduce ME search range, or accept bandwidth increase).

---
**REQ-ARCH-005 — 8K decode not required (single instance)**
- Source/rev: A2 v1.2 · §1.3 · **PDF p.5**
- **[VERBATIM]** "One VCU decoder is **not required** to support 8Kp15 decoding"
- **[INTERPRETATION]** This is a *relief* clause, not a prohibition. Peers must not raise a bug for absent single-instance 8K decode.

---
**REQ-ARCH-006 — Multi-instance 8Kp30**
- Source/rev: A2 v1.2 · §1.3 · **PDF p.5**
- **[VERBATIM]** "In SoCs with 2 VCU instances, 8Kp30 encode and decode **shall** be supported using 'multi-instance' mode."
- Precondition (document-stated): **the SoC contains 2 VCU instances**
- Affected: multi-instance interface (A2 §1.8, PDF p.11 lists "Multi-instance interface")
- **Applicability gate:** DID §Scope **[VERBATIM]** (`did.txt:155`) "It is a hardened block with 1 instance of core encoder and 1 instance of core decoder." **[INTERPRETATION]** If the target SoC instantiates one VCU, REQ-ARCH-006's stated precondition is unmet and the requirement does not apply. **I cannot confirm the instance count for the target part until scope is frozen.** `SPEC_AMBIGUITY L-A03`.

---
**REQ-ARCH-007 — Encoder/decoder separation and isolation** *(HIGH RISK — see §7)*
- Source/rev: A2 v1.2 · §1.5.6 Encoder-Decoder Separation and Isolation · **PDF p.9**
- **[VERBATIM]** "the tile design **shall** maximize separation and isolation… completely un-affected and un-interrupted by any operations and events within the other function, including: hard reset, soft reset, clock rate changes, clock gating, full or partial re-initialization, MCU reset and initialization, error recovery procedures including NMU reset and re-initialization."
- Required: an event in ENC must not affect DEC, and vice versa, across **all nine** enumerated event classes
- Prohibited: any shared mechanism whose activation in one function disturbs the other
- **CONFLICT: L-C03 — the DID itself records this requirement as violated.** See §4.

---
**REQ-ARCH-008 — Multi-stream count and aggregate cap**
- A2 v1.2 · §1.3 · **PDF p.5** · **[VERBATIM]** 32 streams, "Limited to max aggregate throughput of 4Kx2Kp60 minus some overhead"
- **[INTERPRETATION]** The cap is qualified by "minus some overhead" with no figure. `SPEC_AMBIGUITY L-A04` — the legal maximum aggregate load is not a testable number. Peers must not assert a stream-count bug from this clause alone.

---
**REQ-ARCH-009 — Concurrent low-latency streams**
- A2 v1.2 · §1.3 · **PDF p.5** · **[VERBATIM]** "The encoder **shall** support 2 concurrent low latency streams up to 4Kp30 each, one per encoding core"
- Documented limitation, **[VERBATIM]** §1.11.4.1 **PDF p.13**: "the L2 cache is sized for the normal case of both encoder cores sharing a single 4Kx2K stream, each encoding one half. Therefore, the L2 cache is **under-sized for this case**", mitigations = reduce ME search range (quality loss) or accept bandwidth increase.
- **[INTERPRETATION]** The `shall` is functional, not bandwidth-bounded. A ULL bandwidth overshoot is **documented expected behavior**, not a defect.

---
**REQ-ARCH-010 — MCU address space and its resolution**
- A2 v1.2 · §1.5.1 IP Configuration table (**PDF p.7**) and §1.5.3 Solution to MCU Address Space Limitation (**PDF p.8**)
- **[VERBATIM]** §1.5.1: MCU `RISC-V`, `64KB RAM`, **MCU address space `40 bits`**; core address space "48 bits or more"
- **[VERBATIM]** §1.5.3 lists options (a) 48-bit base append, (b) NMU remap up to 32 apertures, (c) custom MMU; "**Option (b) is preferred**"; and the MCU AXI-M ports "should route to a dedicated NMU separate from enc/dec core NMUs"
- **[INTERPRETATION]** "preferred" and "should" are **not** `shall`. This is a design recommendation. A different implementation choice is **not** automatically a bug. `SPEC_AMBIGUITY L-A05` — normativity of the aperture-remap solution is unstated.
- **Contrast:** DID FR **[VERBATIM]** "48-bit address" (`did.txt`, FR list) — the DID states the SoC-side width only and is silent on the 40-bit MCU limitation and on which option was implemented. **Traceability gap L-T01.**

---
**REQ-ARCH-011 — Vendor IP identity**
- A2 v1.2 · §1.5.1 · **PDF p.7** · **[VERBATIM]** Encoder `AL-E200-E`, Decoder `AL-D300-S`; encoder cache `1152KB` (option), decoder cache included standard; FBC option on both; max line width `4K per core`; `2 × 128-bit AXI-M` each; `64x64 CU support: Encoder **No**, Decoder **Yes**`
- **Cross-check to Orion's [STATUS]:** Trace reports implementation strings `E200E`/`D300S` `1p4`. The architecture spec names the **IP part numbers** but **states no IP revision**. `1p4` has no counterpart in either primary document. **Traceability gap L-T02 — no document-stated mapping from DID/arch spec to a vendor IP revision, nor to `rtl/vcu2_v1_7t`.** Answering Orion's request directly: **neither primary document contains any mapping to `vcu2_v1_7t`, to `7t`/`n6` technology strings, or to any vendor drop date.** The DID says only "7nm" (§Scope) — see also L-A06 below.

---
**REQ-ARCH-012 — Clock speed targets**
- A2 v1.2 · §1.5.5 / §1.5.5.1 DCI Support (**PDF p.8–9**), §1.13 (**PDF p.14**), §1.15 (**PDF p.15**)
- **[VERBATIM]** §1.5.5.1: "The VCU **shall** support DCI 4K format"; Encoder 890 → **950** MHz, Decoder 860 → **918** MHz
- **[VERBATIM]** §1.15 table: `VCU Core 950/918`, `MCU Same as IP core`, `AXI master Ports 950/918`, `AXI slave port 167` (all grades), `NPI 300` (all grades). Note **[VERBATIM]** "Other speed grades **shall** follow standard pr2896 curves"
- **[VERBATIM]** §1.15 note: "40% of the VCU-PL interface timing budget is used by VCU, 60% is reserved for PL user logic (30/70 ratio is preferred)"
- **[VERBATIM]** §1.5.5.2 stretch goals: 1120 / 940 MHz
- **[SPEC_AMBIGUITY L-A07]** The §1.15 grade table populates **only** the `-2HP/-3HP/-2LHP` column for VCU Core, MCU and AXI-M. The `-1LP`, `-2LP`, `-1MP`, `-2MP` cells are **blank** for those three domains. Combined with REQ-ARCH-003 ("shall support LP"), **the required VCU core frequency at LP grades is unspecified.** This is a real hole, not an extraction artifact — the AXI-S and NPI rows in the same table *are* populated across all five columns.
- **[VERBATIM]** §1.13.1 Speed and Bitrate Limitations, **PDF p.14**: "There may be a bit rate limitation when VCU runs at low clock frequencies." — hedged ("may"), no threshold. `SPEC_AMBIGUITY L-A08`.

---
**REQ-ARCH-013 — Clock sources; no internal PLL**
- A2 v1.2 · §1.15.1 (**PDF p.15**), §1.30.6 (**PDF p.19**)
- **[VERBATIM]** §1.30.6: "The VCU has no internal PLLs. Clocks **shall** be provided by DPLLs in the GTCC."
- **[VERBATIM]** §1.15.1 source table: VCU core / MCU / AXI master ports ← `GT clocking column DPLL`; AXI slave port ← `NoC`; NPI ← `NPI clock from the GT clocking column`
- Permitted variation, **[VERBATIM]** §1.15: "Aside from NPI, the IP vendor **may** unify clock domains into a single domain."
- Concordance: DID clocking section (`did.txt:531–606`) lists NPI Reference Clock `300` from `GTCC` — **agrees**

---
**REQ-ARCH-014 — VCU Disable via eFUSE; default-disabled after reset** *(HIGH RISK — see §7)*
- A2 v1.2 · §1.20 VCU Disable · **PDF p.16**
- **[VERBATIM]** "Permanent disable via 2 x E-FUSE, one for encoder and one for decoder. The cached E-FUSE values **shall** be provided via NPI as part of the boot process. If more than one VCU instance exists, each instance **shall** be independently programmable to be enabled or disabled. **After reset, by default, VCU shall be disabled until E-FUSE value is transmitted via NPI serial side channel.**"
- **Reset/default semantics (explicit):** post-reset default state = **DISABLED**
- Prohibited: any post-reset state in which the VCU is enabled before the eFUSE value has arrived over the NPI serial side channel
- Software precondition: NPI eFUSE transmission must complete before the block may be considered enabled
- Concordance in DID (**[VERBATIM]**, `did.txt:684`): "Poll for `VCU2_NPI_0.MMD_NPI_PCSR_STATUS.SSC_EFUSE_VALID_STATUS == 1` / If status is not 1, flag error and halt process." — DID supplies the register-level realisation. See REQ-DID-004.
- Interaction hazard, **[VERBATIM]** DID (`did.txt:678`): "If both encoder and decoder are disabled, no register access should be done as SLCR and interconnect would be in reset." → **illegal input** (see §3.3).

---
**REQ-ARCH-015 — Power domain, isolation, no internal power gating**
- A2 v1.2 · §1.18 / §1.18.1 · **PDF p.16**
- **[VERBATIM]** "The VCU **shall** have its own dedicated VCCINT power domain… **There will be no power gating mechanism within the VCU block.** Power down can be achieved via an external power regulator… or by permanently tying VCU power to ground in the package"
- **[VERBATIM]** §1.18.1: "The plan of record for Telluride is to share VCU power rail with **TBD** at the package or the board level… Therefore, the VCU design **shall** support an independent power domain, as well as isolation/clamp/levelshifting."
- **[SPEC_AMBIGUITY L-A09]** The Telluride rail-sharing partner is literally `TBD` **in the v1.2 document**. Note this is the same document family in which the DID's newest edit is described as "Cleaned-up TBDs." — the *architecture* TBD survives.
- Related **[VERBATIM]** §1.3: "when disabled, the VCU **shall** consume no dynamic power"
- Related **[VERBATIM]** §1.21 Power Supplies (**PDF p.16**): "The VCU requires only VCCINT_VCU, core power. There are no analog resources, PLLs, or IOs in the VCU." plus 3 additional small domains: `VCCINT` (PL/NPI interface), `VCCINT_BRAM` (DPLL clocks from GTCC), `VCCINT_SOC` (NoC interface)
- **Open issue upstream:** A2 §1.4 Open Issues row 1 = "Power domain", status **Open**, `TBD` (**PDF p.7**)

---
**REQ-ARCH-016 — Power-On Reset**
- A2 v1.2 · §1.22 · **PDF p.16**
- **[VERBATIM]** "A POR circuit is required, similar to Diablo VCU, to bring the VCU to a known, safe state before PL configuration, regardless of rail power sequencing."
- Required: known safe state established **before PL configuration**, **independent of rail sequencing**
- **[SPEC_AMBIGUITY L-A10]** "known, safe state" is **not enumerated**. No register reset values, no output-pin states, no bus quiescence conditions are given. **This is the single most important undefined term in the architecture spec.** Any peer asserting "POR left the block in a bad state" must cite the DID or the register spec for the concrete state, never this clause. Open DID Issue **#6 BPD POR Instance** (status blank ⇒ open) is unresolved against it.

---
**REQ-ARCH-017 — Error handling** *(HIGH RISK — see §7)*
- A2 v1.2 · §1.25 Error Handling · **PDF p.17**
- **[VERBATIM]** — the section in its entirety — "Errors shall be reported via interrupts."
- **[SPEC_AMBIGUITY L-A11 — SEVERE]** The architecture specification's *complete* error-handling requirement is six words. It defines **no** error taxonomy, **no** correctable/uncorrectable split, **no** status/latch/clear semantics, **no** recovery procedure, **no** timing bound, **no** behavior on error during reset or during eFUSE transfer.
  **Peers: expected error behavior CANNOT be derived from the architecture spec.** It must come from the DID's NPI interrupt sections and the register specifications, and where those are silent the answer is `SPEC_AMBIGUITY`, not an inferred expectation. This is the clause most likely to be mis-cited to manufacture a bug.
  Partial relief from A2 §1.30.5 (**PDF p.18**), **[VERBATIM]**: "the VCU can use NPI interrupts 8, 9, and 10… **Interrupts 8 and 9 are for general messages and correctable error indication, and Interrupt 10 is used for non-correctable or fatal error notification.** The VCU **shall** use the NPI interrupts as part of the initialization procedure." → this is the only normative error *classification* in the document, and it is in the SoC-integration section, not §1.25.

---
**REQ-ARCH-018 — RAS / FUSA**
- A2 v1.2 · §1.28 · **PDF p.17** · **[VERBATIM]** "Reliability and functional safety requirements include: - no special FUSA requirements"
- **[INTERPRETATION]** No FUSA requirement exists to violate. Corroborated by Jira label `FUSA_NO_IMPACT` on EDT-1069519. **Peers must not raise FUSA findings against VCU2.**

---
**REQ-ARCH-019 — Security**
- A2 v1.2 · §1.29 · **PDF p.18** · **[VERBATIM]** "No special security features."
- **Immediately contradicted within the same document** by §1.27.2 Scan Clear (**PDF p.17**) **[VERBATIM]** "Supported. **Scan clear is mainly a security requirement.** Diablo VCU did not support scan clear."; by §1.32 SMID (**PDF p.18**) **[VERBATIM]** "All non-PS masters **must** have securely programmable SMID values. The design **shall** provide programmable SMID. Care **must** be taken to make the SMID assignment secure and hack-resistant using mechanisms such as secure accessibility only, lockable registers, etc."; and by §1.33 AxPROT (**PDF p.19**).
- **CONFLICT: L-C04** (see §4)

---
**REQ-ARCH-020 — SMID**
- A2 v1.2 · §1.32 · **PDF p.18** · **[VERBATIM]** as quoted above
- Required: programmable SMID; secure, hack-resistant assignment; lockable
- Affected: NMU SMID masking ("the VNOC NMU has ability to mask and control incoming SMID values")
- Verification linkage: none

---
**REQ-ARCH-021 — AxPROT / MCU security level** *(ordering constraint)*
- A2 v1.2 · §1.33 · **PDF p.19**
- **[VERBATIM]** "The VCU **shall** provide ability to program the CODEC core as well as the MCU security level, i.e., the secure/nonsecure bit in AxPROT field of the AXI protocol, **separately for encoder MCU and decoder MCU**. The MCU security level **is expected to be static and shall be programmed once during boot or re-initialization** of a VCU instance."
- **Ordering constraint:** program once, during boot or re-init
- **Prohibited (document-stated):** dynamic modification of MCU security level during normal operation
- **Legal/illegal input:** a write to the MCU security level outside boot/re-init is **illegal**
- Concordance: DID Open Issue **#4 (Closed)** **[VERBATIM]** (`did.txt:24`): "Secure SLCR can be used to drive the AXI P bits on AXI Interfaces. Secure SLCR can be programmed via Secure AXI M. AXI signal does not come out from Encoder & Decoder IP." → implementation mechanism = Secure SLCR

---
**REQ-ARCH-022 — Programming model**
- A2 v1.2 · §1.34 · **PDF p.19** · **[VERBATIM]** "- Boot/init via NPI  - Dynamic interaction with software driver via NoC AXI slave port"
- Reinforced **[VERBATIM]** §1.30.5 (**PDF p.18**): "NPI programmable registers **shall** provide basic initialization functionality. During normal operation, encoder programming and software driver interaction **shall** use the NoC NSU path, **not** the NPI and **shall** use PL interrupts to the APU."
- **Prohibited (explicit "not"):** using NPI for normal-operation encoder programming / driver interaction
- **Ordering:** NPI first (boot/init) → NSU thereafter
- Affected: NPI regs (`vcu2_npi_regs`), SLCR regs (`vcu2_slcr_regs`), NSU/AXI-S

---
**REQ-ARCH-023 — Memory access connectivity**
- A2 v1.2 · §1.30.2 · **PDF p.18** · **[VERBATIM]** "All VCU memory access is done via the MMD NoC extension. **There are no PL-based AXI ports.**"
- Prohibited: PL-based AXI memory paths
- Concordance: DID (`did.txt:112`) **[VERBATIM]** "The access to the DDR memory is through MMD NoC extension. Access to the memory does not involve any PL based AXI ports." — **agrees**

---
**REQ-ARCH-024 — Frame buffer formats** *(legal/illegal inputs)*
- A2 v1.2 · §1.35 · **PDF p.19**
- **[VERBATIM]** three formats: "Uncompressed raster / Uncompressed tiled (64x4 or 32x4) / Compressed tile"
- **[VERBATIM]** Encoder: "Default input buffer format: uncompressed tile"; "Optional input buffer format: uncompressed raster"
- **[VERBATIM]** Decoder: "Default output buffer format: uncompressed tile"; "Optional output buffer format: uncompressed raster" — "Requires a 2nd copy of the output frame to be written in raster format, at the cost of extra memory bandwidth"
- **[VERBATIM]** "Encoder input frames and decoder output frames **shall** be in this format [uncompressed tile]. **Any other system agent that produces/consumes these buffers must support the VCU tile format.**"
- **[VERBATIM]** "4:2:0 and 4:2:2 formats use **semi-planar** buffers (one buffer for Y, one buffer for interleaved Cb/Cr), while 4:4:4 format uses **planar** buffers (3 separate buffers for Y, Cb, Cr)."
- **Defaults:** tile in / tile out. **Legal alternative:** raster, at a stated bandwidth cost.
- **[SPEC_AMBIGUITY L-A12]** **[VERBATIM]** "Details of the tiled format and other buffer formats are available in a separate document." — that document is **not named** and is **absent from §1.36 References**. The exact tile geometry contract is therefore unobtainable from the supplied artifact set. Peers must not assert a tiling bug from this document.

---
**REQ-ARCH-025 — AXI master characteristics**
- A2 v1.2 · §1.12 · **PDF p.14** · **[VERBATIM]** encoder AXI-M: data bus width `128 bits`; typical request size `64 to 512 bytes`; burst type `Incrementing`; number of IDs used `small`; max outstanding transactions (rd, wr) `64, 64`
- **[SPEC_AMBIGUITY L-A13]** The `ID width` row is **blank in the source table**, and "small" is not a number. Also, the table is titled *encoder* only — **no decoder AXI characteristics table exists.**
- Framing, **[VERBATIM]**: "The following characteristics are **expected** of the encoder AXI master ports." — "expected", not `shall`. **Non-normative.** A deviation here is not a spec violation.

---
**REQ-ARCH-026 — Bandwidth envelope**
- A2 v1.2 · §1.11 · **PDF p.12–13** · **[VERBATIM]** encoder max at 4Kp60 4:4:4 12-bit `~6GB/s with L2 cache enabled` (+ ~1GB/s per stream raw write at 4Kp60 4:2:0 10-bit, or 2.2GB/s at 4:4:4 12-bit); decoder max at 4Kp60 4:4:4 12-bit `~10-15GB/s` (+ same per-stream read allowance). L2 cache `1152KB`, "encoder max bandwidth is reduced by up to 33% thanks to the cache". **[VERBATIM]** encoder figure note "FBC is enabled on all but the input frame"; decoder figure note "FBC is disabled."
- **[INTERPRETATION]** All figures are `~` estimates. **Non-normative envelope, not a pass/fail limit.** Appendix §3.1 "Memory Bandwidth" is literally `TBD` (**PDF p.21**).

---
**REQ-ARCH-027 — Software base**
- A2 v1.2 · §1.31 · **PDF p.18** · **[VERBATIM]** "VCU **shall** be supported by the existing Diablo VCU and SV60 VDU software base, with enhancements as needed for new functionality."
- **Software precondition** for the whole programme; no version pinned. `SPEC_AMBIGUITY L-A14`.

---
**REQ-ARCH-028 — Memory repair / BIST**
- A2 v1.2 · §1.19 (**PDF p.16**) **[VERBATIM]** "Same as Diablo VCU."; §1.27.3 (**PDF p.17**) **[VERBATIM]** "The VCU contains multiple SRAM memory instances and requires a memory BIST controller. Memory repair support is required. The selection of redundant memory blocks for repair is controlled via E-FUSEs in the PMC, and cached values of the E-FUSEs will be written via NPI as part of the initialization process."
- **[SPEC_AMBIGUITY L-A15]** §1.19 delegates by reference to an unsupplied Diablo document. Same pattern at §1.17 PM (**PDF p.15**) **[VERBATIM]** "Same as Diablo VCU." and §2 (**PDF p.21**) **[VERBATIM]** "See guidelines outlined in the VDU arch specs."
- **Note on document status:** **[VERBATIM]** PDF p.20 carries a hard boundary line: "**No specs beyond this point / The following material is informational only**", placed immediately before §2 and §3. **Nothing in §2 (Memory Subsystem Recommendations), §3.1 (Memory Bandwidth = TBD), §3.2 (Questions to IP Vendor) or §3.3 (DPLL examples) is normative.** Peers must not cite the IP-vendor Q&A table as a requirement.

---
**REQ-ARCH-029 — Interlaced support (vendor answer, INFORMATIONAL)**
- A2 v1.2 · §3.2 Questions to IP Vendor, row 3 · **PDF p.21** · **below the "informational only" line**
- **[VERBATIM]** "Interlaced is supported for HEVC (encode/decode). Interlaced is supported for AVC decode with D105 IP with some limitation (4:2:0 only). Interlaced support for AVC encode is planned for end of Q2 this year (SAFF only)."
- **[INTERPRETATION]** Non-normative and **temporally unanchored** ("this year" in a 2024-dated document). References `D105 IP`, which is **not** the `AL-D300-S` of §1.5.1 — a third IP identifier with no reconciliation. Concords loosely with the DID FR **[VERBATIM]** "interlaced SAFF only".
- `SPEC_AMBIGUITY L-A16`

---

### 3.2 DID requirements and implementation decisions (source A1, `DID@25f514d0`)

---
**REQ-DID-001 — Functional requirement list**
- Source: `DID@25f514d0` · §Introduction → Functional Requirements · `did.txt:48–66`
- **[VERBATIM]** opening line: "VCU Design must meet the following requirements: / **Reference: Everest MMD VCU Arch Specs v1.0**"
- **[VERBATIM]** items: "Dual core encoder and single core decoder"; "Multi-standard encoding support, including: H.264 (AVC – Advanced Video Coding) / H.265 (HEVC – High Efficiency Video Coding)"; "Multi-standard decoding support, including: H.264 / H.265 / **JPEG**"; "Multi-stream support, up to 32 streams per encoder or decoder unit"; "**Max throughput – 4kp60** (both UHD [Ultra HD] & DCI [Digital Camera Initiative] formats)"; "Progressive support"; "Support operation at **MP and HP** voltage levels"; "4 AXI master ports (4 NMU), 1 AXI slave port (1 NSU)"; "Encoder & Decoder unit include 64KB RAM RISC-V controller"; "Support I, IP, and IPB encoding"; interlaced SAFF only; eFuse disable per unit; no PL config bits; 48-bit address; encoder/decoder independent operation, reset and clocks
- **Codec/config answer for Orion:** encode = **H.264 + H.265**; decode = **H.264 + H.265 + JPEG**. **JPEG decode appears in the DID and NOT in the architecture spec's §1.3 summary** → see L-C05.

---
**REQ-DID-002 — Independent ENC/DEC reset** *(HIGHEST RISK — see §7)*
- Source: `DID@25f514d0` · §Resets (`did.txt:633–645`), directly under **[[Heading 2]] Resets**, first line **[VERBATIM]** "EDT-1069519 Updates:", with the anchor `EDT-1069519` hyperlinked (`rId116` → `https://jira.xilinx.com/browse/EDT-1069519`) immediately after the caption **[VERBATIM]** "Figure 11: VCU Reset Generation"
- **[VERBATIM], in full:**
  > "Arch spec requirement is to have ability to individually reset the Encoder (ENC) and Decoder (DEC) cores (incl the RISC-V MCU contained within the cores). **This was missed in the current implementation in which the VCU2 tile has only one source for HW reset (From PL) which will reset the entire VCU2 tile. As a result, failure/hang condition in either ENC or DEC cores will affect both and will require full VCU2 tile reset.** The encoder and decoder functions are completely independent – normal operation of each function will be completely un-affected and un-interrupted by any operations and events within the other function. Thus, we need separate resets for encoder and decoder cores respectively."
  > "Fix is to have **two additional HW reset sources. Spare input pins from PL will be repurposed** and combined with the reset to encoder and decoder blocks. Updates in RED in Figure 11."
  > "These resets need be asserted **only** when the Encoder/Decoder cores experience a hang type situation or end up in an un-recoverable error states and asserting hard reset is the only way to recover. These resets **can be kept in de-asserted state during initialization and in normal operations**."
- **[VERBATIM]** pin table rows:
  > `pl_vcu2_spare_in[56] (ENC RAW RST) | Encoder core reset input from PL. | Serves as reset to Encoder core logic in both L1 and L2.`
  > `pl_vcu2_spare_in[11] (DEC RAW RST) | Decoder core reset input from PL. | Serves as reset to Decoder core logic.`
  > and below: `pl_vcu2_spare_in[56]  Encoder core and MCU` / `pl_vcu2_spare_in[11]  Decoder core and MCU`
- Required behavior: ENC and DEC individually resettable, **including their RISC-V MCUs**, via `pl_vcu2_spare_in[56]` and `pl_vcu2_spare_in[11]`
- **Legal use / precondition (document-stated):** assert **only** on hang or unrecoverable error. Held **de-asserted** during initialization and normal operation.
- **Illegal input:** asserting either raw reset during initialization or normal operation — outside the documented use envelope
- Affected: `vcu2_reset`, `vcu2_int_wrap`, PL spare-pin map, ENC L1+L2, DEC, both MCUs
- Trace: architecture parent = **REQ-ARCH-007** (A2 §1.5.6, PDF p.9). The DID paragraph paraphrases §1.5.6 almost word-for-word ("completely un-affected and un-interrupted by any operations and events within the other function").
- **Internal DID inconsistency:** **Open Issue #7 "VCU Reset — Clarity on VCU Reset Inputs, VCU Reset Implementation" has a blank Status cell (= open)** in the same document that documents the fix as applied. **L-C03 / L-T03.**

---
**REQ-DID-003 — Pre-config sequence (PLM/CDO)** *(ordering constraints — normative sequence)*
- Source: `DID@25f514d0` · §Resets / pre-config sequence, `did.txt:680–700`
- **[VERBATIM]** "The following is the pre-config sequence for VCU2, which will be executed by PLM based on the VCU2 pre-config CDO:"
- **[VERBATIM] Assumptions (= firmware/software preconditions):**
  1. "VCU2, PL, RAM and SoC power domains are turned ON (in any sequence as per chip)"
  2. "pmc_por_rst_b is released"
  3. "NPI interface reset vcu2_npi_preset_n is released"
- **[VERBATIM] ordered steps:**
  1. "VCU2 Power supply status is polled: Check for `VCU2_NPI_0.MMD_NPI_PCSR_STATUS.MMD_PWR_SUPPLY == 1`"
  2. "Unlock VCU2_NPI_0 PCSR: Write unlock code `32'hF9E8D7C6` to `VCU2_NPI_0.NPI_PCSR_LOCK`"
  3. "PMC de-asserts IPOR: `VCU2_NPI_0.MMD_NPI_PCSR_MASK.MMD_IPOR = 1` ; `VCU2_NPI_0.MMD_NPI_PCSR_CONTROL.MMD_IPOR = 0`"
  4. "EDT-1066811: After IPOR is released, enable the functional clock: `VCU2_NPI_0.VCU2_NPI_ECO_REG_2 [2:0] = 3'b010`"
  5. "Secure Efuse transfer. This should be **only after release of IPOR**, but **any time before PL configuration is complete**."
  6. "Poll for `VCU2_NPI_0.MMD_NPI_PCSR_STATUS.SSC_EFUSE_VALID_STATUS == 1` / If status is not 1, **flag error and halt process**."
  7. "DFX isolation is disabled: …"
- **Ordering constraints extracted:** power+POR+NPI-reset released → poll `MMD_PWR_SUPPLY` → PCSR unlock (magic `32'hF9E8D7C6`) → mask-then-clear `MMD_IPOR` → enable functional clock → eFUSE transfer (strictly after IPOR release, strictly before end of PL config) → poll `SSC_EFUSE_VALID_STATUS`
- **Required error behavior (rare, explicit):** eFUSE valid poll failure ⇒ "flag error and halt process"
- **[SPEC_AMBIGUITY L-A17]** **[VERBATIM]** "Assumption here is Step-6 is a concurrent to all the steps which follow it… **Need to confirm this with S/W team.**" — the concurrency of the eFUSE-valid poll with subsequent steps is an **unconfirmed assumption recorded in the DID itself**. Peers must not assume either serialisation or concurrency here.
- Registers named (traceable): `VCU2_NPI_0.MMD_NPI_PCSR_STATUS.{MMD_PWR_SUPPLY, SSC_EFUSE_VALID_STATUS}`, `VCU2_NPI_0.NPI_PCSR_LOCK`, `VCU2_NPI_0.MMD_NPI_PCSR_MASK.MMD_IPOR`, `VCU2_NPI_0.MMD_NPI_PCSR_CONTROL.MMD_IPOR`, `VCU2_NPI_0.VCU2_NPI_ECO_REG_2[2:0]`

---
**REQ-DID-004 — Mem-clear clock switchover sequence** *(ordering constraint, strict)*
- Source: `DID@25f514d0` · `did.txt:664–677`, tagged **[VERBATIM]** "EDT-1066811:" (hyperlinked `rId117` → `jira.xilinx.com/browse/EDT-1066811`)
- **[VERBATIM]** "Memory clear operations will be done with NPI clock. **Before mem-clear is triggered, disable the functional clock, and switch over to NPI clock by following this sequence:**"
  1. `VCU2_NPI_0.VCU2_NPI_ECO_REG_2 [1] = 1'b0` (Disable ICG-2 on FUNC clock)
  2. `VCU2_NPI_0.VCU2_NPI_ECO_REG_2 [2] = 1'b1` (Mem-clear trigger to clk-mux asserted)
  3. `VCU2_NPI_0.VCU2_NPI_ECO_REG_2 [0] = 1'b1` (Enable ICG-1 on NPI clock)
- **[VERBATIM]** "After mem-clear is complete, disable the NPI clock and switch back to functional clock by following this sequence:"
  1. `[0] = 1'b0` (Disable ICG-1 on NPI clock)
  2. `[2] = 1'b0` (Mem-clear trigger to clk-mux de-asserted)
  3. `[1] = 1'b1` (Enable ICG-2 on FUNC clock)
- **Prohibited:** triggering mem-clear while the functional clock is enabled; any other ordering of these three bit-writes
- **[VERBATIM]** rationale (`did.txt:214`): "MEMCLEAR is preferred to be done on functional clocks to speed up the process. For VCU2, RAMs are functionally operated on PL/DPLL/NOC clocks. Since this is a dependency on external block, so the suggestion is to use NPI clock which is expected to be available from the time POR is released."
- **[VERBATIM]** failure behavior: "If mem clear fails, mem_clear_done interrupt remain asserted until SW clears"; and for scan clear "If scan clear fails, scan clear done interrupt remain asserted until SW clears."
- **This is the most precisely specified sequence in the entire artifact set and is the strongest available basis for an ordering-violation finding.**

---
**REQ-DID-005 — Interrupt masking and the unmaskable uncorrectable interrupt** *(required error behavior)*
- Source: `DID@25f514d0` · `did.txt:679–683`
- **[VERBATIM]** "If the block is disabled using eFuse, PMC should write to IMR Registers to mask the interrupts. This will mask the scan clear and mem clear interrupts on the **interrupt line 8 and 9**. On the **interrupt line 10**, the interrupts generated during scan clear fail and mem clear fail will be masked."
- **[VERBATIM]** "The uncorrectable interrupt (**interrupt bit 10 of NPI Interrupts**) is set **immediately after scan clear/memclear is triggered**. **This cannot be masked.** The interrupt will be **self-cleared in VCU after successful completion** of scanclear/memclear. SW has to clear the corresponding ISR bit in PMC."
- **Required behavior:** int[10] asserts on trigger, self-clears on success, SW clears the PMC ISR bit
- **Prohibited:** masking int[10]
- **[SPEC_AMBIGUITY L-A18 — flagged as a likely false-bug source]** The two paragraphs are in tension. The first says int-line-10 scan/mem-clear-fail interrupts "will be masked" when the block is eFuse-disabled; the second says the uncorrectable interrupt on bit 10 "cannot be masked". **[INTERPRETATION]** These may be reconcilable (PMC-side IMR masking vs VCU-side maskability) but **the document does not state the reconciliation.** Any peer raising "int[10] was/wasn't masked" must be challenged on which of these two clauses they are citing.
- Traces to REQ-ARCH-017 / A2 §1.30.5 interrupt classification (8/9 = general + correctable, 10 = non-correctable/fatal)

---
**REQ-DID-006 — Register access illegality when both units disabled** *(illegal input)*
- Source: `DID@25f514d0` · `did.txt:678` · **[VERBATIM]** "If both encoder and decoder are disabled, no register access should be done as SLCR and interconnect would be in reset."
- **Illegal input:** any SLCR/interconnect register access while both ENC and DEC are eFuse-disabled
- **[INTERPRETATION]** A peer observing anomalous behavior on register access in that state has **violated a documented precondition** — this is *not* a bug. **This is a specific, named precondition trap and I expect it to be hit.**

---
**REQ-DID-007 — PL power/functional precondition**
- Source: `DID@25f514d0` · `did.txt:112` · **[VERBATIM]** "**PL is powered on and functional before VCU can be functional.**"
- Precondition on all VCU functional behavior. Corroborates A2 §1.22 (POR before PL configuration) as a *distinct* earlier gate.

---
**REQ-DID-008 — ABUS Switch DPSTx2 / SysMon voltage measurement**
- Source: `DID@25f514d0` · **[[Heading 6]] Abus Switch DPSTx2** (`did.txt:427`) and the surrounding SysMon section (`did.txt:421–462`)
- **[VERBATIM]** "VCU2 is required to provide a feature for voltage measurement (of VCU2 supply). The SysMon for the voltage measurement (`ams_abus_switch_DPSTx2`) is in VCU2. It supports the measurement of two power supplies – package supply & to monitor irdrop of same power supply on Metal5."
- **[VERBATIM]** "In VCU2, we have one abus switch and it monitors two sense points. The switch receives sense point data from the package and irdrop on Metal 5."
- **[VERBATIM]** defect + fix (`did.txt:460–462`): "**Reify simulation issues assertion that there is a floating signal in AMS DPSTx2 switch.** The signal (psush_por_b) is used to control an isolation cell present inside the AMS DPSTx2 switch. PMC releases the existing isolation control (psuph_por_b) after PL and AUX supplies are UP. **It does check for the VCU supply to be UP before releasing this isolation control. Because of this there will be instantaneous contention current during Power-UP and Power-Down.**" / "To fix this, use the PL-VCU2 isolation control signal equivalent, generated by the VCU2 BPD PoR instance, for isolation control inside of AMS ABUS Switch, instead of existing top-level input (psuph_por_b). **An Analog OR gate in AUX power domain to pass iso_vcu2_pl_n.**"
- **[SPEC_AMBIGUITY L-A19 — apparent typo in source]** The DID spells the signal **`psush_por_b`** in one sentence and **`psuph_por_b`** in the next two. Also note **[VERBATIM]** "It does check for the VCU supply to be UP" — read in context (the sentence explains *why* there is contention), the intended meaning is almost certainly "does **not** check". **I am not correcting the DID.** Both anomalies are reported as-is; `rtl-tracer` should resolve the true signal name from RTL, and the negation must be confirmed with the author before anyone relies on it.
- **[VERBATIM]** "`vcu2_ams_abus_ctrl_if` comes from PL power domain. Level shifter is added to the interface before it enters `vcu2_int_wrap` module. And `vcu2_ams_sense_abus_if` & `vcu2_psuph_por_b` are from AUX power domain."

---

### 3.3 Consolidated defaults / legal-illegal / ordering / error tables

**Reset and default behavior (only what is document-stated):**

| Item | Default / reset state | Source |
|---|---|---|
| VCU enable | **Disabled** after reset, until eFUSE value arrives via NPI serial side channel | A2 §1.20 p.16 |
| POR target state | "known, safe state" — **unenumerated** (`L-A10`) | A2 §1.22 p.16 |
| ENC/DEC raw resets `pl_vcu2_spare_in[56]/[11]` | De-asserted during init and normal operation | DID §Resets |
| Encoder input buffer format | Uncompressed tile | A2 §1.35 p.19 |
| Decoder output buffer format | Uncompressed tile | A2 §1.35 p.19 |
| FBC (encoder path) | Enabled on all but the input frame | A2 §1.11.2 p.12 |
| MCU security level (AxPROT) | Static; set once at boot/re-init | A2 §1.33 p.19 |
| Internal power gating | **None exists** | A2 §1.18 p.16 |
| Register reset values | **Not in either primary document** — see `L-T04` | — |

**Illegal inputs / precondition violations (challenge ammunition):**

| # | Illegal action | Source |
|---|---|---|
| I1 | Register access while both ENC and DEC eFuse-disabled | DID `did.txt:678` |
| I2 | Asserting `pl_vcu2_spare_in[56]/[11]` during init or normal operation | DID §Resets |
| I3 | Triggering mem-clear without the 3-step FUNC→NPI clock switchover | DID (EDT-1066811) |
| I4 | Any ordering of the `VCU2_NPI_ECO_REG_2[2:0]` writes other than the documented one | DID (EDT-1066811) |
| I5 | Secure eFUSE transfer **before** IPOR release, or after PL config completes | DID pre-config step 5 |
| I6 | Changing MCU security level outside boot/re-init | A2 §1.33 p.19 |
| I7 | Using NPI for normal-operation encoder programming / driver interaction | A2 §1.30.5 p.18 |
| I8 | Producing/consuming VCU frame buffers without supporting the VCU tile format | A2 §1.35 p.19 |
| I9 | Exercising VCU functionally before PL is powered and functional | DID `did.txt:112` |

**Required error behavior (the complete document-stated set):**

| Condition | Required behavior | Source |
|---|---|---|
| Any error | "reported via interrupts" (no further detail) | A2 §1.25 p.17 |
| Correctable / general | NPI interrupts 8 and 9 | A2 §1.30.5 p.18 |
| Non-correctable / fatal | NPI interrupt 10 | A2 §1.30.5 p.18 |
| scan-clear / mem-clear triggered | int[10] set immediately; **unmaskable**; self-clears on success; SW clears PMC ISR bit | DID `did.txt:682` |
| scan clear fails | scan-clear-done interrupt stays asserted until SW clears | DID `did.txt:666` |
| mem clear fails | `mem_clear_done` interrupt stays asserted until SW clears | DID `did.txt:677` |
| `SSC_EFUSE_VALID_STATUS != 1` | "flag error and halt process" | DID pre-config step 6 |

That is the entirety of specified error behavior. **Anything else is `SPEC_AMBIGUITY`.**

---

## 4. Requirement Conflicts

### L-C01 — LP voltage level: architecture requires it, DID drops it *(MEDIUM-HIGH)*
- A2 v1.2 §1.3 (**PDF p.5**) **[VERBATIM]** "Support operation at **LP, MP and HP** voltage levels"
- `DID@25f514d0` §Functional Requirements **[VERBATIM]** "Support operation at **MP and HP** voltage levels"
- The DID silently narrows a `shall`-class capability. **Aggravating evidence:** A2 §1.15 (**PDF p.15**) leaves the `-1LP` and `-2LP` VCU-Core/MCU/AXI-M frequency cells **blank** — so the architecture spec demands LP support but never states an LP frequency target (`L-A07`).
- **[INTERPRETATION]** Two readings, both plausible, neither documented: (a) LP was descoped by an undocumented decision → **missing waiver**; (b) LP is still required and the DID is stale against v1.2. **Cannot be resolved from the artifacts. Requesting a waiver/decision record from Orion.**
- Note this conflict is *also* explicable by the version gap: the DID binds v1.0 (L-M02), and I have not read v1.0. **If v1.0 says "MP and HP", the DID is faithful to its own parent and the conflict is arch-v1.0-vs-v1.2, not DID-vs-arch.** This distinction matters and I flag it rather than pick a side.

### L-C02 — 8K requirements absent from the DID *(HIGH)*
- A2 v1.2 §1.3 (**PDF p.5**) contains **three** 8K clauses (REQ-ARCH-004/005/006), two of them `shall`.
- `DID@25f514d0` §Functional Requirements states **[VERBATIM]** "Max throughput – 4kp60" and **contains no 8K requirement anywhere.**
- Same two readings as L-C01, plus a third: 8K may have entered the architecture spec **between v1.0 and v1.2**, in which case the DID is stale by construction. **This is the single most consequential item riding on `L-A01` (was v1.2 released?) and on obtaining v1.0.**
- **Requesting `Everest_MMD_VCU_Arch_Specs_1.0.pdf`** — without it, L-C01, L-C02 and L-C05 cannot be adjudicated.

### L-C03 — Encoder/decoder isolation `shall` recorded as violated in the DID *(HIGHEST)*
- Requirement: A2 v1.2 §1.5.6 (**PDF p.9**), REQ-ARCH-007
- Admission: `DID@25f514d0` §Resets **[VERBATIM]** "**This was missed in the current implementation** in which the VCU2 tile has only one source for HW reset (From PL) which will reset the entire VCU2 tile."
- Fix recorded in the same section (2 repurposed PL spare pins), tied to **EDT-1069519**.
- Authoritative Jira (opened directly, `jira.xilinx.com`): **EDT-1069519 "Independent Reset for Encoder and Decoder" — Status `Closed`**, Assignee Galagali, Abhay, Labels `FUSA_NO_IMPACT`, `rtl_risk`, created 2024-03-05, updated 2024-07-15. Description **[VERBATIM]** "as part of the Architecture spec, it details to have an independent hardware reset for Encoder and Decoder, the present implementation is single hardware reset for both Encoder and Decoder. independent reset is possible using software resets. which Allegro mentioned that current soft resets available shall work similarly like VCU1. Need to choose b/w the existing S/W solution is feasible or two have independent H/W reset as requirement from architecture spec." Closing comments **[VERBATIM]** "From Functional verif side, the reset seems to be working as expected"; "checked in the waves and the intended testcases are passing".
- **Residual conflict:** the DID's own **Open Issue #7 "VCU Reset — Clarity on VCU Reset Inputs, VCU Reset Implementation" remains open (blank Status)** in the same revision that documents the fix. Jira closed; DID open-issue table not updated. **Document-control conflict, not necessarily a design conflict.**
- **`rtl_risk` label is a standing flag for `rtl-tracer` and `proof-gate`.** The verification evidence in Jira is comment-level assertion ("waves checked", "testcases passing") with **no test name, no coverage item, no VSpec** (L-M09). Treat as **weak** closure evidence.

### L-C04 — "No special security features" vs three security `shall`/`must` clauses *(MEDIUM)*
- A2 v1.2 §1.29 (**PDF p.18**) **[VERBATIM]** "No special security features."
- Contradicted in the same document by §1.27.2 (**PDF p.17**, scan clear "is mainly a security requirement"), §1.32 (**PDF p.18**, SMID "must"/"shall", "hack-resistant", "lockable registers"), §1.33 (**PDF p.19**, AxPROT programmable secure/nonsecure per MCU).
- **[INTERPRETATION]** §1.29 most likely means "no *block-specific* cryptographic features", but it does not say so. As written it is a blanket negation that a peer could cite to dismiss a genuine SMID or AxPROT finding — **or** to manufacture one. **Peers: do not cite §1.29 in either direction.** Cite §1.32/§1.33 for security requirements. `SPEC_AMBIGUITY L-A20`.
- Corroboration that security *is* in scope: Confluence XSIC/792798238 v87 has a dedicated **Security** review column on all three tables (VCU row: `incomplete`).

### L-C05 — JPEG decode in the DID, absent from architecture §1.3 *(LOW-MEDIUM)*
- DID FR **[VERBATIM]** "Multi-standard decoding support, including: H.264 / H.265 / **JPEG**"
- A2 v1.2 §1.3 summary does not list JPEG among the required codecs.
- **[INTERPRETATION]** DID broadening rather than narrowing. Direction matters: an *extra* capability in the DID is a scope-addition question (who authorised it, is it verified), not a spec violation. Possibly present in v1.0 or in the unread Allegro decoder specs (DID references `Decoder Specs`, `rId29`, a SharePoint folder). `SPEC_AMBIGUITY L-A21`.

### L-C06 — IPB **decoding** unspecified while IPB **encoding** is required *(MEDIUM)*
- DID FR **[VERBATIM]** "Support I, IP, and IPB **encoding**"
- DID **Open Issue #2**, Status **blank (= open)**, **[VERBATIM]** (`did.txt:22`): `2 | | Support I, IP, and IPB decoding | In the architecture specs, nothing is mentioned about IPB decoding.`
- I confirm the DID's own claim against the primary source: **A2 v1.2 contains no IPB decoding requirement.** §1.9.2 (**PDF p.11**) says only **[VERBATIM]** "the decoder reads the compressed bit-stream from memory, and, along with **0, 1, or 2 additional reference frames** from frame buffers, decodes the current frame" — "2 additional reference frames" *implies* B-frame capability but **never states an IPB decode requirement**.
- **`SPEC_AMBIGUITY L-A22`. There is no authoritative expected behavior for IPB decoding. Any IPB-decode bug hypothesis must be rejected on requirements grounds unless a peer produces a source I have not seen.** This is a direct answer to Orion's request for the "email-resolved decoder single-core and I/P/IPB passages": **the DID's open-issue table is the only place these appear, both rows have empty Status cells, and neither cites an email or resolution. If an email resolution exists, it is not in the DID.**

### L-C07 — Encoder cache as general-purpose memory: architecture `TBD`, DID treats as feature *(LOW)*
- A2 v1.2 §1.3 (**PDF p.5**) marks encoder-cache-as-general-purpose-memory **TBD**; §1.8 (**PDF p.11**) hedges **[VERBATIM]** the AXI-S port is for "external programming and **possible** encode-cache reuse as SRAM"
- **[INTERPRETATION]** Not a requirement. `SPEC_AMBIGUITY L-A23`. No expected behavior exists to test against.

### L-C08 — MCU selection: architecture optional list vs fixed choice *(informational)*
- A2 v1.2 §1.3 **[VERBATIM]** "Include embedded micro controller unit (MCU) such as Arm, MicroBlaze or RISC-V"; §1.5.1 table fixes `RISC-V`, `64KB RAM`; §3.2 row 6 (informational) **[VERBATIM]** "Plan is to use RISC-V, synchronous with respect to the IP engines."
- DID: `RISC-V`, `64KB RAM`. **No conflict** — the §1.3 list is an allowance, resolved by §1.5.1. Recorded so no peer raises it.

### L-C09 — DID Scope says "1 instance of core encoder" while the same document requires dual-core *(editorial)*
- DID §Scope **[VERBATIM]** "It is a hardened block with **1 instance of core encoder** and 1 instance of core decoder."
- DID FR **[VERBATIM]** "**Dual core** encoder and single core decoder"; DID §Encoder Subsystem **[VERBATIM]** "VCU comprises **one instance of dual core encoder**"
- **[INTERPRETATION]** Reconciled by the third quote: *one encoder instance, which is dual-core*. §Scope's phrasing is loose, not contradictory. Recorded to pre-empt a false conflict.

### L-C10 — Challenge resolved: EDT-1072959 is **not** a DID error *(closed by me)*
Raised by Echo via Orion: Jira EDT-1072959 is titled "Reify Assertions Failures for the Power Aware Scenarios", not ABUS/DPSTx2.
**Investigation, at the XML level:**
- The DID revision cell is **Table index 0, row r7, column c3**, rendered text **[VERBATIM]** `"EDT-1069519 fix: Updated reset section.\nEDT-1072959 fix: Updated Abus Switch DPSTx2 section"`. Two paragraphs in one cell, `\n`-separated. **The cell contains no hyperlink** (`links=[]`), so the ID is plain typed text in the source.
- Neighbouring cells r7c0=`1.5.1`, r7c1=`07/15/2024`, r7c2=`Abhay Galagali`. **No merged cells; the table is a clean 4-column grid; rows r2 and r3 are empty but structurally intact.** Row alignment is therefore **not** corrupted, and the `[[TABLE]]` extraction reproduces the cell faithfully — I verified rendered text against a direct `document.tables[0]` cell dump.
- Elsewhere in the DID, `rId86` → `https://jira.xilinx.com/browse/EDT-1072959` is anchored on the literal text `EDT-1072959` in the **ABUS Switch section**, immediately after **[VERBATIM]** "Figure 14: ABUS Switch Instantiation … `vcu2_ams_abus_ctrl_if` comes from PL power domain…". So the document links that Jira ID to that section in a second, independent place.
- The DID text at that location reads **[VERBATIM]** "**Reify simulation issues assertion** that there is a floating signal in AMS DPSTx2 switch."
**Verdict: no mismatch, no corruption, no OCR artifact, and nothing for the DID to correct.** Jira EDT-1072959's title describes the *symptom* (Reify power-aware assertion failure); the DID's revision comment describes the *section that was edited* to record the fix (ABUS Switch DPSTx2). They are the same issue viewed from two ends. **I recommend Echo withdraw this item.** (Direct `get_issue` re-fetch of EDT-1072959 returned `JIRA HTTP 429: Rate limit exceeded` on my attempt; the above rests on the DID XML plus Echo's reported title, so the *title* itself is **[UNVERIFIED-RELAY]** from Echo — everything else is first-hand.)

---

## 5. XRDB Traceability — BLOCKED

**`XRDB-UNAVAILABLE`.** All attempts (`xrdb_list_projects`, `xrdb_search`, 3+ tries across the session) return:
`XRDB projects error: Unable to connect. Is the computer able to access the url?`

Consequently **none** of the mandated XRDB searches could be executed: requirements without RTL links; requirements without verification links; partial/orphaned requirements; duplicated requirements; conflicting values; stale document links; incorrect release applicability. **This section is a known, declared hole in the deliverable — it is not "no issues found".**

**Interim substitute (clearly labelled NON-XRDB).** On-disk register artifacts located under the supplied RTL root `…/VCU_VERIF_12MARCH/shadow/rtl/vcu2_v1_7t/`:
- `vcu2_slcr_regs/regspecs/vcu2_0_slcr_regs.{h,rtf,svh,tcl,xml}`, `vcu2_1_slcr_regs.{h,rtf,svh,tcl,xml}`, `vcu2_slcr.ods`, `vcu2_slcr_regs.sv`
- `vcu2_slcr_regs/rtl/vcu2_slcr_regs.{filelist,sv}`
- `vcu2_npi_regs/rtl/vcu2_npi_regs.{filelist,sv}`, `vcu2_npi_regs_wrapper.sv`
- `rtl/vcu2_v1_7t/regspecs/toplevel/`

**Two `vcu2_0_` and `vcu2_1_` SLCR register spec sets exist**, consistent with a two-VCU-instance-capable register map — **relevant to REQ-ARCH-006 / `L-A03`** (multi-instance 8Kp30), and worth a peer check.

### Traceability gaps recorded so far (source-derived, not XRDB-derived)

| ID | Gap |
|---|---|
| L-T01 | REQ-ARCH-010 (MCU 40-bit address space, NMU aperture remap "option (b) preferred") has **no corresponding DID section**. The DID states "48-bit address" and never mentions the 40-bit MCU limitation or which option was implemented. |
| L-T02 | **No document-stated mapping** from either primary document to `rtl/vcu2_v1_7t`, to technology strings `7t`/`n6`, or to vendor IP revision `1p4`. The arch spec names `AL-E200-E`/`AL-D300-S` with **no revision**; the DID says only "7nm". *(Direct answer to Orion's [STATUS] request.)* |
| L-T03 | DID Open Issue **#7 (VCU Reset)** open while the fix it concerns (EDT-1069519) is Jira-`Closed` and documented in the same DID revision. |
| L-T04 | **No register reset values in either primary document.** REQ-ARCH-016's "known, safe state" therefore has no concrete definition anywhere in the supplied artifact set. Must come from XRDB (down) or the on-disk regspecs. |
| L-T05 | **Zero verification linkage for every requirement in §3** — no VCU VSpec exists (L-M09). Every record's "Verification linkage" field reads *none*. |
| ~~L-T06~~ | **RETRACTED IN FULL — see §12.3.** The claim that the Starting-References table has empty Document Link cells, and that "the DID does not link its own register database", was an artifact of my defective extractor. The DID **does** link XRDB (`http://zynq_design:8070/`), an Encoder Specification and a Decoder Specification. Replaced by **L-T06R** in §12.3. |
| L-T07 | DID Open Issues **#1, #2, #6, #7, #14, #15, #16 have blank Status cells** (= open) in a revision whose changelog says "Cleaned-up TBDs." Seven open issues survived the TBD cleanup. |
| L-T08 | A2 §1.35 references an unnamed "separate document" for tile-format details; it is **not in §1.36 References** (`L-A12`). |
| L-T09 | A2 delegates §1.17, §1.19 to "Same as Diablo VCU" and §2 to "the VDU arch specs" — **three unsupplied documents in the normative chain**. |

---

## 6. Waivers, Errata and Late Decisions

**No waiver register has been located.** Neither primary document contains a waiver or deviation section. Confluence XSIC/792798238 v87 tracks *approval*, not *deviation*.

**Late decisions found, first-hand:**
| Ref | Nature | Evidence |
|---|---|---|
| **EDT-1069519** | Architecture-deviation-then-fix: single tile HW reset → 2 repurposed PL spare pins (`pl_vcu2_spare_in[56]`/`[11]`). Jira **Closed** 2024-07-15, labels `FUSA_NO_IMPACT`, `rtl_risk`. | Jira opened directly; DID §Resets |
| **EDT-1072959** | ABUS DPSTx2 isolation-control change: use `iso_vcu2_pl_n` from VCU2 BPD PoR via an analog OR in AUX, instead of top-level `psuph_por_b`. | DID §Abus Switch DPSTx2 + `rId86`; Jira title **[UNVERIFIED-RELAY]** (429) |
| **EDT-1066811** | Clock-management ECO: `VCU2_NPI_ECO_REG_2[2:0]` FUNC/NPI clock switchover for mem-clear; functional-clock enable after IPOR. | DID pre-config + mem-clear sequences; `rId117` |
| EDT-1056613, EDT-1059821, EDT-1069146, EDT-1077095 | Referenced in the DID; **not yet opened**. | DID hyperlinks |
| Allegro DVT tickets **4285**, **4858** | Vendor-side issues linked from the DID (`tickets.allegrodvt.com`). **Not reachable from this session.** | DID `rId96`, `rId97` |
| PSEV-23069 | DID review tracker for VCU. **Not yet opened.** | Confluence v87 |

**Candidate live errata** (Jira search, earlier in session — **[UNVERIFIED-RELAY]** pending individual `get_issue`): `CR-1276805 "[T40][VCU2] Decoder SCD stalls partway through start-code scan"` (Assigned), `CR-1275059`, `CR-1276696`, `CR-1276868`, `CR-1276900`, `EDT-1098905`. **Note CR-1276805 carries a `[T40]` tag — the first device-variant signal seen anywhere, and it is in Jira, not in the specs.**

**No errata, waiver or late decision was found that alters REQ-ARCH-003 (LP), REQ-ARCH-004/006 (8K) or REQ-ARCH-019 (security).** The L-C01/L-C02 conflicts therefore stand unwaived on present evidence.

---

## 7. High-Risk Normative Requirements

Ranked by (normative strength) × (evidence of deviation) × (blast radius).

1. **REQ-ARCH-007 / REQ-DID-002 — encoder/decoder isolation and independent reset.** A nine-clause `shall`; the DID states in writing it "was missed"; the fix rides on **repurposed PL spare pins**; Jira closed on comment-level verification with **no VSpec**; the DID's own Open Issue #7 is still open; Jira label is `rtl_risk`. **Every peer should look here first.**
2. **REQ-ARCH-014 — eFUSE disable and default-disabled-after-reset.** Explicit reset default, explicit boot-order dependency, an explicit halt-on-error, and a documented trap (I1) where register access is illegal. High chance of both real bugs and false bugs.
3. **REQ-DID-004 — mem-clear FUNC↔NPI clock switchover.** Exact bit-level ordering on `VCU2_NPI_ECO_REG_2[2:0]`, in both directions. The most testable ordering requirement in the artifact set.
4. **REQ-ARCH-017 + REQ-DID-005 — error reporting and interrupt maskability.** A six-word architecture requirement (`L-A11`) plus an internally tense DID passage on int[10] maskability (`L-A18`). **Highest false-bug risk in the investigation.**
5. **REQ-ARCH-016 — POR "known, safe state".** Normative but undefined (`L-A10`), with DID Open Issue #6 (BPD POR) still open and no register reset values anywhere (`L-T04`).
6. **REQ-ARCH-021 / REQ-ARCH-020 — AxPROT static-once-at-boot and secure programmable SMID.** Crisp `shall`, crisp prohibition, plausible RTL exposure, and undermined by the §1.29 blanket negation (`L-C04`).
7. **REQ-ARCH-022 — programming model split (NPI for boot only, NSU for operation).** Contains an explicit "not", which is rare and therefore enforceable.
8. **REQ-ARCH-004/006 — 8K.** Potentially the largest missing-capability finding, but **gated entirely on `L-A01`**. Do not act until the v1.0/v1.2 release question is settled.

---

## 8. Ambiguous or Missing Requirements — index

| ID | Subject | Nature |
|---|---|---|
| L-A01 | Was arch spec v1.2 formally released? | Unresolvable from this session; needs SharePoint listing of `Arch Specs (released)/Telluride/MMD/` |
| L-A02 | Multi-core decoder | DID Open Issue #1 open; requirement itself clear |
| L-A03 | Instance count for the target part | Gates REQ-ARCH-006 |
| L-A04 | "4Kx2Kp60 minus some overhead" | Untestable aggregate cap |
| L-A05 | Normativity of NMU-aperture MCU remap ("preferred"/"should") | Recommendation, not `shall` |
| L-A06 | Technology/part mapping | Only "7nm"; no T-variant, no stepping, in either document |
| L-A07 | VCU core/MCU/AXI-M frequency at LP and MP grades | Table cells blank |
| L-A08 | Bitrate limit at low clock | "may", no threshold |
| L-A09 | Telluride VCU rail-sharing partner | Literal `TBD` in v1.2 |
| L-A10 | POR "known, safe state" | Undefined — **critical** |
| L-A11 | Error handling | Six words total — **critical** |
| L-A12 | Tile format details | Points to an unnamed, unreferenced document |
| L-A13 | AXI ID width; decoder AXI characteristics | Blank cell; no decoder table; section is "expected", non-normative |
| L-A14 | Software base version | Unpinned |
| L-A15 | "Same as Diablo VCU" delegations | Unsupplied documents |
| L-A16 | Interlaced support | Informational only; temporally unanchored; cites a third IP (`D105`) |
| L-A17 | eFUSE-poll concurrency in pre-config | DID: "Need to confirm this with S/W team" |
| L-A18 | int[10] maskability | Two DID clauses in tension — **high false-bug risk** |
| L-A19 | `psush_por_b` vs `psuph_por_b`; probable missing "not" | Source typos; **not corrected by me** |
| L-A20 | "No special security features" | Blanket negation contradicted in-document |
| L-A21 | JPEG decode | In DID, absent from arch §1.3 |
| L-A22 | IPB **decoding** | No requirement exists anywhere — **reject IPB-decode bugs on requirements grounds** |
| L-A23 | Encoder cache as general-purpose memory | `TBD` / "possible" |

---

## 9. Responses to Peer Hypotheses

| Peer | Item | LEDGER position |
|---|---|---|
| Echo (via Orion) | EDT-1072959 title ≠ ABUS DPSTx2 ⇒ DID corruption? | **Rejected — L-C10.** Cell verified at XML level: plain text, no hyperlink, no merged cells, no row misalignment. Jira title describes the Reify assertion symptom; DID §ABUS Switch DPSTx2 records the same issue's fix and independently hyperlinks the same ID. Recommend withdrawal. |
| Prover (via Orion) | DID reference table pins arch v1.0 under "Arch Specs (released)" while also linking a version-agnostic folder | **Confirmed and strengthened — L-M02.** Both facts are in a single `rId13` URL: `id=` names `Everest_MMD_VCU_Arch_Specs_1.0.pdf` inside `Arch Specs (released)`; `parent=` is the version-agnostic MMD folder. Additionally the v1.0 pin appears a second time as prose at the head of §Functional Requirements. |
| Prover / Orion | Was v1.2 released, or did the DID go stale? | **Not resolvable from available evidence, and I will not infer it from RAG — L-A01.** Best available datum: the authoritative tracker's VCU arch row points at `VCU2_Eval_Report_Summary_rev2.pdf`, not at any VCU arch spec, so its silence on v1.2 is weak evidence. Needs a SharePoint directory listing. |
| Orion | Decoder single-core and I/P/IPB "email-resolved" passages | **No email resolution exists in the DID.** Both live only in the Open Issues table (rows 1 and 2), **both with empty Status cells**, quoted verbatim in L-C06 / REQ-ARCH-002. If an email resolution exists it is outside the supplied artifacts. |
| Orion | Telluride-vs-Everest naming | **Not a conflict — same programme, two names.** A2 §1.1 (**PDF p.4**) **[VERBATIM]**: "The MMD VCU is part of the **Telluride** family of products. The MMD VCU is sometimes referred to as **VCU2**, to distinguish it from the original **Diablo** (Zynq Ultrascale+) VCU." DID §Scope **[VERBATIM]**: "…Telluride Video Codec Unit… based on the **Everest (Telluride)** MMD VCU architecture requirements." Everest = programme/platform; Telluride = product family within it; MMD = Multi-Media Distributed tile; VCU2 = this block, vs Diablo VCU (VCU1). **The naming risk is not ambiguity — it is that "Everest" appears in document titles while "Telluride" appears in scope statements, so a search on one name misses artifacts filed under the other.** Evidence: the DID is titled *Telluride*_DID and the arch spec is titled *Everest*_MMD, and they are parent and child. |
| Orion | Mapping from DID/arch to `vcu2_v1_7t`, `7t`/`n6`, `E200E`/`D300S 1p4` | **No such mapping exists in either primary document — L-T02.** Arch spec gives IP part numbers with no revision; DID gives "7nm" only. Trace's implementation strings cannot be validated against requirements. |
| Orion | Do not compare against RTL yet | **Complied.** No RTL file has been opened for comparison; the RTL tree is cited only as an inventory of register-spec artifacts (§5) after XRDB went down. |
| Sentinel | Publish exact citations; state opened-vs-relayed | **Delivered** in §1, §2, §3 (page numbers for the PDF, heading+table-index+line for the DOCX, page id + version + row/column for Confluence). Opened directly: both primary files (byte-level, hashed), Confluence 792798238 v87, Jira EDT-1069519, DID OOXML relationships. Relayed/unverified: EDT-1072959 title (429), the CR-127xxxx errata list, and anything attributed to Trace/Prover/Echo. |
| Sentinel | "Do not mark no-issue without inspected evidence" | **Complied.** §5 is declared BLOCKED, not clean. Every "no conflict" verdict (L-C08, L-C09) carries its verbatim basis. |

---

## 10. Sources Searched

**Opened directly (first-hand):**
- `Telluride_DID_1.5_VCU.docx` — full body text, all tables, and the OOXML relationship map (`word/_rels/document.xml.rels`) for hyperlink targets
- `Everest_MMD_VCU_Arch_Specs_1.2 (1).pdf` — **all 22 pages**, §1.1 through §1.36 plus §2 and §3 appendix
- Confluence **XSIC/792798238 v87** (live)
- Jira **EDT-1069519** (live, `jira.xilinx.com`)
- RTL tree inventory at `…/VCU_VERIF_12MARCH/shadow/rtl/vcu2_v1_7t/` (directory listing + regspec file inventory only; **no RTL read for comparison**) and `shadow/.icmconfig` (`P4CLIENT=yadav+everest+v1+35`, `P4PORT=xhdicmsuper:1777`)

**Searched, discovery only, results not relied upon:** Docs RAG (`search_docs`) for VCU arch spec revisions and DID variants; Jira JQL sweeps for VCU2 CR/EDT items.

**Attempted and failed:**
- **XRDB** — `xrdb_list_projects`, `xrdb_search`: `Unable to connect` (3+ attempts). **HARD BLOCKER for §5.**
- **`p4`** — binary not on PATH (`timeout: failed to run command 'p4': No such file or directory`). **The RTL changelist / branch identity cannot be established.** Requesting a `p4` module path or the sync CL from the workspace owner.
- **Jira EDT-1072959** — `HTTP 429: Rate limit exceeded`. Will retry.
- **SharePoint** — no read path from this session. Blocks `L-A01`.
- **Allegro DVT tracker** (tickets 4285, 4858) — external, unreachable.

**Not yet done (declared, not silently omitted):** arch spec **v1.0** and **v1.1** (needed to adjudicate L-C01/L-C02/L-C05); DID sections on Address Space, Interrupts, Clocking, Power Domains, Top-Level Ports and AXI/APB/Security interfaces at full depth (headings mapped, normative content partially extracted); Jira EDT-1056613 / EDT-1059821 / EDT-1069146 / EDT-1077095 / PSEV-23069; the CR-127xxxx errata set; on-disk regspec XML parse as the non-XRDB traceability substitute.

---

## 11. Standing Rulings for Peers

1. **Never cite A2 §1.25 ("Errors shall be reported via interrupts") as the expected behavior for any specific error.** It cannot support a finding. Use §1.30.5 (int 8/9/10 classification) or the DID's scan/mem-clear passages, or declare `SPEC_AMBIGUITY`.
2. **Never cite A2 §1.29 ("No special security features")** in either direction — L-C04.
3. **IPB decoding has no requirement.** Reject on requirements grounds — L-C06 / L-A22.
4. **FUSA findings are out of scope for VCU2** — A2 §1.28, corroborated by the `FUSA_NO_IMPACT` Jira label.
5. **Anything below PDF page 20's "No specs beyond this point / informational only" line is non-normative** — including the IP-vendor Q&A on interlaced support and all DPLL examples.
6. **A "shall" the DID omits is not thereby removed.** The DID is subordinate to the architecture requirements by its own Scope statement. Omission ⇒ suspected undocumented descope ⇒ waiver required. No waiver register exists.
7. **A DID/arch mismatch may be an arch-v1.0-vs-v1.2 artifact, not a DID defect.** The DID binds v1.0 (L-M02). Until v1.0 is in hand, do not attribute L-C01/L-C02/L-C05 to the DID.
8. **Check the illegal-input table (§3.3, I1–I9) before proposing any bug.** Several documented preconditions — especially I1 (register access with both units eFuse-disabled) and I2 (raw resets outside hang recovery) — will otherwise produce false findings.
9. **No requirement in this document has verification linkage.** There is no VCU verification specification (L-M09). "Verified" claims must cite a test, not a document.

---

# 12. REV B — CORRECTIONS AND SUPPLEMENT

**Rev B date:** 2026-08-12. **Reason:** a methodological defect in my own extraction was found and fixed. Rev B retracts two findings, revises the manifest, and adds a large body of newly recovered normative content. Nothing in the DID changed; my reading of it did.

## 12.1 FINDING L-M11 — the DID carries unaccepted tracked changes throughout, and my Rev A extractor silently dropped all of them

**Severity: methodological — this invalidates any Rev A statement of the form "the DID is silent on X" that was based on table content.**

`python-docx` `cell.text` / `paragraph.text` concatenate only `<w:t>` inside *ordinary* runs. Runs wrapped in `<w:ins>` (tracked insertion) and `<w:del>` (tracked deletion, whose text lives in `<w:delText>`, not `<w:t>`) are **skipped without warning**. The DID is saved with changes *unaccepted*: XML census over `word/document.xml` — `del` 2172, `delText` 894, `bookmarkStart` 2892, `tc` 1197.

Consequence: **28 of the DID's 46 tables reported "100% empty" in Rev A and are not empty.** They are wholly composed of tracked-change runs.

Corrected extractor `/tmp/ledger_extract/deltext.py` walks `tc.iter(w:r)`, tests `r.getparent().tag` against `w:ins` / `w:del`, and concatenates both `w:t` and `w:delText`. Two views are now generated:

| View | File | Size | Meaning |
|---|---|---|---|
| `current` (accepted) | `/tmp/ledger_extract/did_current.txt` | 100,356 B / 1,766 lines | insertions kept, deletions dropped — what the DID says *if you accept all changes* |
| `all` (with markers) | `/tmp/ledger_extract/did_all.txt` | 190,706 B / 2,250 lines | everything, `{INS:…}` / `{DEL:…}` tagged |

Both contain all 46 tables. Rev A's `did.txt` (1,238 lines) is **superseded and must not be cited**.

**L-M11 is itself a first-order requirements defect, independent of my tooling:** *"the DID says X" is presently ambiguous between two different documents.* A DID released with unaccepted tracked changes has no single authoritative text. Any downstream reader using Word's default "Simple Markup" view sees the accepted text; any reader using a script, a diff tool, or "All Markup" sees a different document. **Recommendation to Orion: treat the DID as NOT baselined until changes are accepted and it is re-released.**

## 12.2 REVISED L-M01 — corrected revision history (T00, all-markers view)

```
r0: Version | Date       | Editor          | Version Comments
r1: 0.5.0   | 06/20/2022 | Anurag Agrawal  | Initial draft
r2: {INS:0.9.0} | {INS:10/27/2022} | {INS:Anurag Agrawal} | (comment cell empty)
r3: {INS:1.0.0} | {INS:11/15/2022} | {INS:Anurag Agrawal} | (comment cell empty)
r4: 1.1.0   | 02/10/2022 | Anurag Agrawal  | BPD POR implementation
r5: 1.2.0   | 08/20/2023 | Anurag Agrawal  | Sysmon Implementation
r6: 1.5.0   | 09/10/2023 | Anurag Agrawal  | Interrupt Tree Diagram / DFX Arch Updates /
                                             NPI Interrupt – Scan clear & mem clear updates /
                                             Partial reconfiguration proposal / Area Estimation Updates
r7: 1.5.1   | 07/15/2024 | Abhay Galagali  | EDT-1069519 fix: Updated reset section.
                                             EDT-1072959 fix: Updated Abus Switch DPSTx2 section
r8: 1.5.1   | 31/10/2025 | Abhay Galagali  | Cleaned-up TBDs.
```

Surviving anomalies (all **stand**):
- **Duplicate version number `1.5.1`** on r7 and r8, 15 months apart, with different content deltas. There is no way to cite "DID 1.5.1" unambiguously.
- **Mixed date formats**: r1–r7 are `mm/dd/yyyy`; r8 is `31/10/2025`, which can only be `dd/mm/yyyy`.
- **Out-of-sequence entry**: `1.1.0` dated `02/10/2022` follows `1.0.0` dated `11/15/2022`.
- **New:** rows r2/r3 (`0.9.0`, `1.0.0`) are *still-unaccepted insertions* — the revision history itself is mid-edit, and both comment cells are genuinely blank.
- **New:** the filename (`1.5`) matches no row in the table.

## 12.3 L-T06R — corrected Starting References table (T03) with resolved URLs

Rev A's L-T06 was wrong twice over: the cells are not empty, **and** the links are not `w:hyperlink` elements — they are **legacy field-code hyperlinks** (`w:instrText` containing `HYPERLINK "…"`), which is why my first URL-extraction pass printed nothing. `hyperlink` element count in T03 = **0**; `instrText` count = **26** (13 targets + 13 field terminators).

| Row | Type (accepted view) | Link text | Resolved target |
|---|---|---|---|
| r1 | Architecture Specifications | VCU Arch Specs | `amdcloud.sharepoint.com/:f:/r/sites/eng/processing solutions/everest/des_ver/Shared Documents/Telluride/VCU/Arch specs` — **folder** |
| r2 | Design Guidelines | Xilinx Security Best Practices | `…/Modules/PMC/Design for Security - Best Practices.docx` |
| r3 | Design Specifications | Clock and Reset | `…/Modules/CRX - Clock and Reset/Everest_Microarch_CRX.docx.aspx` |
| r3 | Design Specifications | Everest DFX Controller v1.8 | `…/Telluride/VCU/DFX_DID/DID_DFx_Cntrlr_Everest_V1.8.docx` |
| r3 | Design Specifications | NPI Protocol Interface | `…/fdst/everest/arch/Shared Documents/Arch Specs (released)/Global Functions/NPI Protocol Interface` — **folder** |
| **r4** | **VCU Register Database** | `{INS:XRDB}` | **`http://zynq_design:8070/`** |
| **r5** | `{INS:Encoder + }MCU` | `{INS:VCU Encoder Specification}` over `{DEL:RISC – V Specs specs link (TBD)}` | `…/Telluride/VCU/Encoder Specs` — **folder** |
| **r6** | `{INS:Decoder + MCU}` (row inserted) | `{INS:VCU Decoder Specification}` | `…/Telluride/VCU/Decoder Specs` — **folder** |
| r7 | VCU SharePoint Site | VCU Design & Verification | `…/des_ver/Shared Documents/Forms/AllItems.aspx?…` |
| r8 | Everest Config & Reconfig Spec | `{INS:Everest Configuration and Reconfiguration}` over `{DEL:link}` | `…/Arch Specs (released)/Global Functions/Configuration and Reconfiguration` — **folder** |
| r9 | Everest Chip Pervasive Features | Chip Pervasive spec | `…/fdst/everest/arch/Restricted Documents/Forms/AllItems.aspx?…` |
| r10 | Xilinx BISR cache | Xilinx BISR Cache | `…/Modules/PMC/XLNX_BISR_CACHE` — **folder** |
| r11 | NPI Arch Spec | NPI Arch spec | `…/Global Functions/NPI Protocol Interface/NPI_Arch_Spec_v1.1.pdf` |

**L-T06R (replacement finding) — the reference table is real but structurally unciteable.**
1. **Six of thirteen targets are SharePoint *folders*, not documents.** Rows r1 (Architecture Specifications), r5 (Encoder), r6 (Decoder), r8 (Config/Reconfig), r10 (BISR) and one Global-Functions row name **no file and no revision**. A folder reference cannot establish authority, revision or approval status. This is `SPEC_AMBIGUITY`, and it is the reason Atlas could not find the encoder/decoder behavior: **the DID never names an `encoder_specification.pdf`.** It points at a directory.
2. **Only two targets are version-pinned in the URL**: `DID_DFx_Cntrlr_Everest_V1.8.docx` and `NPI_Arch_Spec_v1.1.pdf`. Everything else is a floating pointer to whatever the location currently holds.
3. **Domain split — two link generations coexist.** The field-code targets are all `amdcloud.sharepoint.com`. The relationship-table targets (`word/_rels/document.xml.rels`) include `xilinx.sharepoint.com` — e.g. `rId29` → `xilinx.sharepoint.com/…/Modules/VCU/Decoder Specs`, and `rId13` → `xilinx.sharepoint.com/…/Arch Specs (released)/Telluride/MMD/Everest_MMD_VCU_Arch_Specs_1.0.pdf`. The DID therefore carries **two different decoder-spec pointers on two different tenants**, and its only version-pinned architecture-spec pointer is to **v1.0**, while the copy supplied to this investigation is **v1.2**. `L-A01` (was arch v1.2 released?) is unchanged and now sharper: the DID's own link says v1.0.
4. `rId34` points at `http://xinc/ppg/…/Montana/fe/Modules/VCU/Eng Spec/VCU_APM_Specification.docx` — a **Montana**-program path in a Telluride DID.

**Accessibility (answering Orion's [REQ] directly):** every SharePoint target above requires interactive authentication; I have no read path to either tenant from this session. **BLOCKED — exact URLs published above so a credentialed agent or the user can open them.** Docs RAG is not a substitute and I have not treated it as one.

**XRDB is the exception — it is reachable.** `http://zynq_design:8070/` returns **HTTP 200**; DNS `zynq_design → 172.23.155.30 (xhdengvm155030.xilinx.com)`; page title `Register Database`; it advertises its own documentation at `amd.atlassian.net/wiki/spaces/XPS/pages/1183525513/XRegDB+-+XRDB+Home`. **So the Rev A statement "XRDB is down" is imprecise and is hereby narrowed: the XRDB *MCP connector* is down; the XRDB *service* is up.** The site is a form-POST UI (`action="/"`) with no discoverable REST/JSON endpoint (`/getProjects`, `/listProjects`, `/api/projects`, `/static/db/device_reference/projects_map.txt` all return 404). §5's traceability gaps remain open, but the blocker is now a *tooling* blocker with a known-good host, not an outage.

## 12.4 FINDING L-M12 — the DID simultaneously contains a deleted VDU-era design and its VCU-era replacement

This is the largest structural finding in Rev B and it supersedes the Rev A naming-residue note (which cited only Open Issue #11).

Recovered from the previously-"empty" tables:

- **T15 / T16 / T17 and their surrounding prose are wholly `{DEL:…}`** — an entire "NPI Interface for VDU CORE" / "NPI registers for VDU" section marked for deletion.
- **T13 / T14 are the surviving VCU-era replacements** for T15 / T16.

**T13 (kept) vs T15 (deleted) — the NPI signal list lost six signals:**

| Signal in deleted T15 | Present in kept T13? |
|---|---|
| `sys_rst1_n` (Async, 1, I, "Slave reset from PMC to VCU") | **NO** |
| `sys_rst2_n` (Async, 1, I, "Slave reset from PMC to VCU") | **NO** |
| `sys_rst3_n` (Async, 1, I, "Slave reset from PMC to VCU") | **NO** |
| `slv_clk` (-, 1, O, "npi_ref_clk from VCU back to NPI Endpoint") | **NO** |
| `coe_id` (Async, 5, O, "Not required. Obsolete.") | **NO** |
| `npi_opt_1` (Async, 1, I, drives `addr_size`; "The use of this input signal can be eliminated.") | **NO** |

**This is the direct answer to Chronos's INV-RESET-ISOLATION-01 spare-reset question, and it is a negative-with-evidence, not a silence:** the three PMC slave resets `sys_rst1_n/2_n/3_n` were **present in the VDU-era DID text and deleted, not migrated.** The accepted view of the DID therefore specifies **no** `sys_rst*` inputs at all — no AXI disposition, no IRQ/status, no clock requirement, no recovery/reinit, because the signals themselves are gone from the surviving text. Whether the RTL still carries them is Tracer's question; if it does, **the requirement basis for them has been deleted from the DID without a recorded decision.** I found no waiver, no Jira reference and no rationale attached to the deletion.

Also lost in the T15→T13 migration: the `addr_size` tie-off *instruction*. Deleted T15 said "*This output is driven using npi_opt_1 power signal from GTCC. Recommendation is to connect these bits to required value (3'b010) at VCU integration level*"; kept T13 retains only "*This output is driven at VCCINT_SOC. Value = 2 (indicates 128kB).*" The **binding integration instruction was dropped while the value was kept** (`REQ-DID` gap; the deleted text cites `EDT-1010078`). Deleted prose also recorded the unresolved fork: "*Npi_opt1 is not required in VCU. We have VCCINT_SOC power domain. (To be reviewed)*" — marked *to be reviewed*, then deleted rather than resolved. **SPEC_AMBIGUITY.**

**VDU naming residue survives into the accepted view** — this is not confined to deleted text. In **T14, the kept VCU-era table**:

| Field | Verbatim comment in T14 (accepted view) |
|---|---|
| `INITSTATE` (PCR, POR, 1 bit) | "For issuing reset to **VDU**." |
| `PWR SUPPLY STATUS` (PSR, –, 1 bit) | "Indicates **VDU** supply is ready. This bit is required for PLM" |
| `Isolation control` (PCR, POR, 3 bits) | "For PMC to control isolation gasket removal. Until PMC writes to these registers, **VDU** isolation is not removed" |

And in the deleted-but-informative prose: the register source of truth is given as an **ODS file, not XRDB** — "*Full list of register can be found in VDU ODS. ODS File Path: `$SW_ROOT/vdu_7t_n1/registers/vdu_npi.ods` (Update the Path) (To do)*". Note the string **`vdu_7t_n1`** — this is the only `*_7t` identifier I have found in either primary document, and it is a **VDU** path, not `vcu2_v1_7t`. **I therefore still have no DID/arch statement mapping the design to `vcu2_v1_7t` or to any vendor IP revision** (Orion's standing question); the nearest artifact is a deleted, admittedly-stale ODS path bearing a different name.

Similarly **T10** (eFuse) and **T40** (249-row top-level port list) are VDU-named: `Efuse[44] → VDU0 (bottom most) — "Set this bit to 0 to enable VDU0"`, `Efuse[45] → VDU1`, `Efuse[46] → VDU2`; ports `pl_vdu_axi_dec_clk`, `pl_vdu_axi_mcu_clk`, `pl_vdu_core_clk`, `pl_vdu_mcu_clk`, `pl_vdu_axi_lite_clk`, `pl_vdu_mcu_vdec_debug_clk/_update`. Meanwhile the **body prose** mandates the opposite convention: "*the naming convention for ports would be starting with `noc_vcu2_*`, `vcu2_noc_*` … `pl_vcu2_*`, `vcu2_pl_*`*".

**L-M12 ruling: the DID's port table, eFuse table and NPI register comments are VDU-derived and contradict the DID's own stated VCU2 naming convention. Do not use T10 or T40 as a port/eFuse requirement source for VCU2 without independent confirmation.** For Chronos: Table 10's blankness was **my extraction artifact, not a source defect** — but its recovered content is VDU, which is a source defect of a different and larger kind.

## 12.5 Newly recovered normative content — reset, isolation and power sequencing (accepted view)

Recovered because it sits in tracked-change runs. Cited to `did_current.txt` line numbers.

- **Two BPD POR instances.** "*The in-house developed BPD POR IP takes three power inputs only to generate POR. So, to monitor 4 voltages, two instances of the IP is planned to use*" (`:430`). Instance 1 is in the **PL** domain and monitors PL/VCU/RAM (`:432`); instance 2 is in the **VCU** domain and monitors VCU/SOC/RAM (`:438`).
- **Mandated power-on-reset sequence** (`:444–471`), in order: instance 1 detects PL+VCU+RAM → releases `vcu2_por_rst_int_n` → routed to instance 2 **through ELS1** → instance 2 detects SoC+VCU+RAM → releases `vcu2_por_rst_n` → level-shifted **through ELS4** to PL → reported to PMC as supply status via NPI slave registers → PMC polls power status → **PMC de-asserts IPOR after power ramp completes** → "*Release of IPOR releases reset on isolation control signals in PCSR*" → PMC de-asserts the five PCSR isolation controls `ctrl_iso_0_mmd_pl_dfx`, `ctrl_iso_1_mmd_pl`, `ctrl_iso_2_mmd_ram`, `ctrl_iso_3_mmd_noc`, `ctrl_iso_4_mmd_noc_dfx` (all `1 → 0`).
- **Isolation is a two-term AND** (`:465–466`): "*Iso_pl_vcu2_n is generated by ANDing the signals `hw_iso_pl_vcu2_n` & `ctrl_iso_1_mmd_pl`*"; "*`iso_vcu2_pl_n` is generated by taking `Iso_pl_vcu2_n` through ELS5*". So hardware (POR-derived) **and** software (PCSR-written) must both permit; neither alone.
- **Naming inconsistency inside the isolation set (`SPEC_AMBIGUITY`, L-A24):** the prose enumerates the PCSR controls as `ctrl_iso_*_mmd_*` (`:459–463`) but the signals they gate as `ctrl_iso_3_vcu2_noc` / `ctrl_iso_4_vcu2_noc_dfx` (`:435–436`). **`mmd` and `vcu2` are used interchangeably for what appear to be the same five controls, and the DID never states they are the same.**
- **Sequence-step defect (L-A25):** the line "*First instance of BPD POR asserts the control signal `hw_iso_pl_vcu2_n` (0→1)*" appears **three times** as a distinct numbered step (`:451`, `:468`, `:471`), interleaved with a fourth, different assertion "*…asserts the control signal `hw_iso_vcu2_pl_n` (0→1)*" (`:455`). One signal is asserted three times in one sequence and the reciprocal once. Either the sequence is wrong or steps were copy-pasted during the VDU→VCU2 edit. **Ordering constraint is therefore not reliably specified.**
- **Isolation control table (T20, `:476–485`)** — recovered in full; 9 controls with per-signal release conditions. `iso_pl_vcu2_dfx_n` (CTL_0) releases `scan_clear_clock`, `scan_clear_trigger`; `iso_vcu2_pl_dfx_n` releases `scan_clear_done`, `scan_clear_pass`, `scan_done_interrupt`; `iso_pl_vcu2_n` / `iso_vcu2_pl_n` (CTL_1); `iso_ram_vcu2_n` (CTL_2); `iso_vcu2_noc_n` / `iso_noc_vcu2_n` (CTL_3); `iso_noc_vcu2_dfx_n` / `iso_vcu2_noc_dfx_n` (CTL_4). Note the **asymmetric release condition** for CTL_1: `iso_pl_vcu2_n` releases "*when PL, VCU and RAM power are up*" but `iso_vcu2_pl_n` releases "*when VCU and RAM power are up*" — PL is **not** required in the reverse direction. Flagged as a candidate ordering hazard; the DID gives no rationale.
- **Clamping (`:1032`):** "*Reset signals are proposed to clamp with asserted state values (reset is in asserted State when PL power down)*". The word **"proposed"** in a released DID = `SPEC_AMBIGUITY` (L-A26); this is a normative clamp value stated non-normatively. Feed-through scan signals clamp to **1** (`:322`).
- **Spare port ranges (`:332–344`)**, VCU2-named and 5 bits each: `input [4:0] noc_vcu2_spare`, `output [4:0] vcu2_noc_spare`, `output [4:0] vcu2_pl_spare`, `input [4:0] pl_vcu2_spare`. **One bit is load-bearing:** "*`pl_vcu2_spare_out [4]` is the overall IRQ signal. It is OR of `vcu2_enc_pintreq`, `vcu2_dec_pintreq` and SLCR IRQs*" (`:890`). Note the port is declared `vcu2_pl_spare` but referenced as `pl_vcu2_spare_out` — **third naming inconsistency** (L-A27).
- **Interrupt-mask reset dependency (`:875`):** "*The reset for Interrupt mask registers is dependent on INITSTATE*"; "*Mask the general messages and correctable interrupts at reset*" (`:884`).
- **eFuse (`:735`):** "*Out of the 48 bits received from eFuse, [40:37] are used to determine whether encoder or decoder needs to be disabled or brought out of reset.*" **Conflicts with T10**, which assigns `Efuse[44..47]` to VDU0..VDU3. Two different eFuse bit-fields for the same disable function — one VCU2-era prose, one VDU-era table. **L-C11.**
- **SSC lockout (`:773`):** "*Once locked out, the SSC receiver state can only be cleared through the `por_pl_b` from PMC.*" — a hard recovery precondition, and the only documented recovery path for that state.
- **IPOR ordering precondition, stated twice verbatim on one line (`:545`, repeated `:775`):** "*IPOR has to be released to start ssc efuse packets transfer, otherwise No efuse Error, valid interrupts can be captured by Interrupt status register.*" The duplication on `:545` is a copy-paste artifact; the sentence is also **semantically malformed** ("otherwise No efuse Error, valid interrupts **can** be captured" — the intended sense is almost certainly *cannot*). **L-A28 `SPEC_AMBIGUITY`: as written, the stated error behavior is the opposite of what the surrounding text implies. Do not use this sentence as an expected-behavior oracle without vendor confirmation.**
- **Partial reconfiguration (`:836–839`):** "*VCU2 is not required to anything extra to support PR. When the PL region connected to VCU2 is undergoing PR, PMC would shut down VCU. After PR is complete, VCU would be reinitialized. Expectation is VCU will gracefully start working after being reinitialized.*" — "**Expectation**" and "**gracefully**" are undefined; there is no reinit register sequence, no completion indication and no timeout. **L-A29.** This is the closest the DID comes to a recovery/reinit requirement and it is not testable as written.
- **Address space (`:842`):** the address set is delegated to an external spreadsheet, `VCU2_Address_Map.xlsx`, **which is not in the references table (T03) and not supplied**. **L-T10.**

## 12.6 EDT-1072959 / ABUS DPSTx2 — original passage, answering Sentinel

Verbatim from the original (`did_current.txt:832–834`, under [Heading 6] "Switch instantiation", following "Figure 14: ABUS Switch Instantiation"), heading "**EDT-1072959 updates:**":

> "Reify simulation issues assertion that there is a floating signal in AMS DPSTx2 switch. The signal (**psush_por_b**) is used to control an isolation cell present inside the AMS DPSTx2 switch. PMC releases the existing isolation control (**psuph_por_b**) after PL and AUX supplies are UP. It does check for the VCU supply to be UP before releasing this isolation control. Because of this there will be instantaneous contention current during Power-UP and Power-Down."
>
> "Reify simulation assertions are triggered only when the simulation is run with vccint_vcu supply is OFF and vccint_aux supply is ON. Not seen in simulations in which both these supplies are ON."
>
> "To fix this, use the PL-VCU2 isolation control signal equivalent, generated by the VCU2 BPD PoR instance, for isolation control inside of AMS ABUS Switch, instead of existing top-level input (psuph_por_b). An Analog OR gate in AUX power domain to pass `iso_vcu2_pl_n`."

Two defects **confirmed from the original**, not relayed:
1. **`psush_por_b` vs `psuph_por_b`** — two spellings in adjacent sentences for what must be one signal. The surrounding text elsewhere uses `vcu2_psuph_por_b` (`:831`), so **`psush_por_b` is a typo**; but a requirements document that names the subject of a fix two different ways cannot be cited precisely. **L-C10 confirmed, upgraded from relayed to verified.**
2. **"It does check for the VCU supply to be UP before releasing this isolation control. Because of this there will be instantaneous contention current"** — as written the causal claim is inverted: *checking* for VCU supply would prevent, not cause, contention. The intended sense is evidently "*It does **not** check*". **L-A30 `SPEC_AMBIGUITY` — the stated root cause is self-contradictory as written.** This is the second dropped negation found in this document (cf. L-A28), which is itself a signal about review quality.

## 12.7 EOF / `sync_eof` — RESOLVED, and the authority is Allegro, not the DID

Rev A reported a negative result: zero `eof|sync_` hits in the DID, and one non-normative mention in the arch spec (§1.8, PDF p.11, "Sync interface to the PL for slice-level low latency mode"). **That negative result stands for the two primary documents.** The contract exists elsewhere and I have now located it.

**Authoritative source: `EDT-1094051` — "Sync EOF should last exactly one clock cycle"** (jira.xilinx.com). Status **Closed**; assignee Erusalagandi, Srikanth; created 2025-12-15; updated 2026-07-31. Verbatim description:

> "As per VCU2 specification, `sync_eol` (end of 16 pixels lines) one clock cycle pulse and `sync_eof` (end of frame) **at least one clock cycle** pulse on the last Eol pulse of the frame. But **Allegro has clarified that both `sync_eol` and `sync_eof` pulses should last exactly 1 encoder clock cycle**. The encoder clock is 950 MHz and PL clocks can't be that high and also won't be in phase. So `sync_eof` generated by PL DMA Engine IP like Framebuffer Write which are running ~300 MHz won't be accurate."
>
> Vendor references: `https://tickets.allegrodvt.com/issues/5011`, `https://tickets.allegrodvt.com/issues/6213`.

**This is a requirement conflict of exactly the kind in my mandate — a late vendor decision silently overriding a written spec. L-C12:**

| | Statement | Source | Authority |
|---|---|---|---|
| A | `sync_eof` = **at least** one clock cycle | "VCU2 specification" (as quoted in EDT-1094051; **the quoted spec is not the DID or Arch v1.2** — neither contains the string) | written spec, **document not identified** |
| B | `sync_eof` = **exactly** one encoder clock cycle | Allegro clarification, via EDT-1094051 → Allegro 5011 / 6213 | vendor IP owner, **late decision** |

A and B are not reconcilable: "at least one" admits multi-cycle pulses that "exactly one" prohibits. **Ruling for Bug-Breaker and Proof-Gate: a multi-cycle `sync_eof` is a violation under B and compliant under A. Any bug filed against pulse width must cite B and must state that B originates in a vendor ticket, not in an approved AMD requirements document.** I have not been able to identify which document supplies statement A — it is neither the DID (0 hits) nor Arch v1.2 (0 hits). **L-A31: the "VCU2 specification" quoted by the assignee is unidentified; most likely the unversioned SharePoint Encoder Specification folder from T03 r5. Unresolved.**

**Answers to remaining constraint questions Orion asked (clock domain, width, edge, core mapping, reset, loss/duplication):**
- **Width:** exactly 1 encoder clock cycle (per B). **Encoder clock = 950 MHz** — the first concrete figure for it I have found in any source.
- **Clock domain:** the encoder clock, explicitly **not** the PL clock. The ticket states PL clocks "can't be that high and also won't be in phase" — so a PL-generated `sync_eof` is **inherently non-compliant** without a re-timing stage. That is the defect.
- **Verified operating points:** the fix was verified with `sync_eof` driven at **330, 300, 150, 75 and 37.5 MHz**, with assertions checking single-cycle generation (comments by Ammula, Girish Kumar and Kalluri, Punnaiah Choudary).
- **Observable state:** register **`src_sync` at `0xE8040060`**. **`0x13FF` = one frame received and processed successfully; `0x33FF` = failing case** (Erusalagandi, Srikanth). Cross-check: this is consistent with the DID's NSU→interconnect base `0xE8000000` (`did_current.txt`, Interconnect section), i.e. offset `0x40060`. **This is a concrete, citable pass/fail oracle — the only one I have found in this investigation.**
- **Silicon status:** "Tested Single Frame on Presilicon with **T50 Fix**. Tested Resolution: 320x240. VCU was able to process partial frame at every EOL and EOF signal until it finishes one complete single frame."
- **Secondary defect found during verification:** "Found the **Frame Buffer Index is rolling once per frame** and has been fixed."
- **Edge behavior, core mapping, reset behavior, loss/duplication semantics: STILL NOT SPECIFIED in any source I can reach.** `SPEC_AMBIGUITY` — reported, not filled in.

**L-C13 — the same defect is Closed on one ticket and open on another.** A text search for `sync_eof`/`sync_eol` returns 120 issues; the directly relevant cluster:

| Key | Summary | Status |
|---|---|---|
| **EDT-1094051** | Sync EOF should last exactly one clock cycle | **Closed** |
| **EDT-1096956** | Sync EOF should last exactly one clock cycle *(identical title)* | **Assigned — OPEN)** |
| EVVNC-82998 | EDT-1094076: Sync EOF Duration - VCU0 and VCU1 | Test Started |
| CR-1271467 | Add flop for `sync_eof` inside FBW to fix LLP hang issue in VCU LLP design | Closed |
| ER-28337 | Need to update FB_WR IP to force 1 extra EOF signal at the end | To Do |

Two observations I can support: (i) **the remedy is in the PL Frame-Buffer-Write IP, not in the VCU** — CR-1271467 adds a flop *inside FBW*, ER-28337 changes *FB_WR IP*; so a VCU-side RTL difference here would **not** be the bug. (ii) **EDT-1096956 is open with the identical title to a Closed ticket, and EDT-1094076 (referenced by EVVNC-82998) is a third key in the same family.** Whether these are duplicates, per-instance splits (VCU0/VCU1), or a reopened regression **I cannot determine** — see the tooling limit below. **Do not treat EDT-1094051's Closed status as evidence the issue is fixed for the target release.**

## 12.8 Jira field availability — explicit answer to Orion and Sentinel, no inference

**Requested fields that this connector does not return, for any issue:** `affectedVersions` (`versions`), `fixVersions`, `issuelinks` / duplicate graph, linked changelist / commit / review, backport or revert records, waiver records, and attachment metadata or content. The `search` action strips output to key/summary/status; the `get_issue` action returns summary, status, assignee, components, labels, created, updated, description and truncated comments **only** — the version and link fields are silently absent, not empty. I requested them explicitly by name and they did not appear. **These fields are UNAVAILABLE via `mcp__sage__search_xilinx_jira`; they are not "empty in Jira", and no one should record them as empty.** Retrieving them needs direct REST (`/rest/api/2/issue/EDT-1094051?fields=fixVersions,versions,issuelinks,attachment`) or the web UI, neither of which I have a path to.

**Attachment accessibility:** EDT-1094051's description embeds `!image-2025-12-15-18-09-12-922.png!`. **I cannot retrieve or view it.** I am not characterizing its contents, and no waveform description should be attributed to me.

**Allegro tickets 5011 and 6213** (and 4285/4858 linked from the DID): `tickets.allegrodvt.com` is a vendor-external tracker. I have **not** attempted to reach it — I have no authorization to authenticate to a third-party vendor system, and doing so is outside read-only access to AMD-internal sources. **BLOCKED pending explicit authorization; URLs published above.**

**The four issues Orion named:** `EDT-1094051` retrieved (above). `EDT-1096956`, `EDT-1076244`, `EDT-1069519` and `EVVNC-82998` were requested and **all four returned `JIRA HTTP 429: Rate limit exceeded`** — a transient tooling limit from issuing four calls in parallel, not a permissions or existence result. They will be retried serially. **`EDT-1069519`'s substance is already independently established from the DID original** (revision-history r7: "EDT-1069519 fix: Updated reset section") and §12.5 above is that reset section, read from the source.

**On the contradictory security labels — reporting, not inferring.** EDT-1094051 carries all four of `IMT_baseline_difference`, `fixed_in_imt50`, `security_review_closed`, `security_review_inprogress`. `security_review_closed` and `security_review_inprogress` are **simultaneously present and mutually exclusive on their face**. I could not retrieve a label dictionary, a workflow definition, or the issue's changelog, so **I cannot establish what any of these four labels means, who applies them, or in what order.** Jira labels are additive free-text and are not removed automatically by workflow transitions, so accumulated history is *a* plausible explanation — **that is a hypothesis, explicitly not a finding, and it must not be used to conclude the security review is complete.** `fixed_in_imt50` and `IMT_baseline_difference` reference an "IMT"/"imt50" baseline that appears **nowhere in the DID or the arch spec**; combined with "T50 Fix" in the verification comment, IMT-50 is plausibly a T50-related build baseline, but **the DID/arch documents establish no such mapping and I will not assert one.** **L-A32.**

## 12.9 Rev B negative results that stand

- **Backpressure, flush, abort, DMA error, timeout, watchdog:** zero hits across the full tracked-change-aware text of both primary documents (`did_all.txt`, `arch_spec.txt`). Confirmed against the corrected extraction, not the defective one. Genuinely absent.
- **Email resolutions:** zero hits for `e-?mail|per discussion|as discussed|agreed|resolution` in `did_all.txt`. **No supplemental email resolution is recorded anywhere in the DID**, including in deleted text — so no email specifies spare-reset AXI disposition, IRQ/status, clock requirements, or recovery/reinit. Chronos's question is answered in the negative, now against the complete text.

## 12.10 Rev B additions to the ambiguity index

| ID | Ambiguity |
|---|---|
| L-A24 | `ctrl_iso_*_mmd_*` vs `ctrl_iso_*_vcu2_*` used interchangeably for the same five PCSR controls; equivalence never stated. |
| L-A25 | Power-on sequence asserts `hw_iso_pl_vcu2_n (0→1)` as three separate steps; ordering not reliably specified. |
| L-A26 | Reset clamp values are "**proposed**", not mandated, in a released DID. |
| L-A27 | Port declared `vcu2_pl_spare`, referenced as `pl_vcu2_spare_out`. |
| L-A28 | IPOR/SSC sentence appears to have a dropped negation; as written the error behavior is inverted. |
| L-A29 | PR reinit: "Expectation is VCU will **gracefully** start working" — no sequence, no completion indication, no timeout. |
| L-A30 | EDT-1072959 root cause as written ("It **does** check … Because of this there will be contention") is self-contradictory; second dropped negation. |
| L-A31 | The "VCU2 specification" quoted in EDT-1094051 as saying "at least one clock cycle" is unidentified; not the DID, not Arch v1.2. |
| L-A32 | `IMT`/`imt50` baseline referenced by Jira labels has no definition in any supplied requirements artifact. |

## 12.11 Rev B additions to conflicts and traceability

| ID | Conflict / gap |
|---|---|
| L-C11 | eFuse disable field: prose says bits **[40:37]** of 48 select encoder/decoder disable; T10 says **Efuse[44..47]** select VDU0..VDU3. Two incompatible field definitions. |
| L-C12 | `sync_eof` width: written spec "**at least** one clock cycle" vs Allegro "**exactly** one encoder clock cycle". Late vendor decision overrides written spec; the written spec's host document is unidentified. |
| L-C13 | `sync_eof` defect is **Closed** (EDT-1094051) and **open** (EDT-1096956, identical title), with EDT-1094076/EVVNC-82998 in test. Fix status per release is indeterminate. |
| L-T10 | `VCU2_Address_Map.xlsx` is the sole authority for the VCU address set, is not in the T03 references table, and is not supplied. |
| L-T11 | Register source of truth is stated in deleted text as an **ODS file** (`$SW_ROOT/vdu_7t_n1/registers/vdu_npi.ods`, itself marked "(Update the Path) (To do)"), while T03 r4 points at **XRDB**. Two register authorities, one stale, no statement of precedence. |

## 12.12 Standing rulings added in Rev B

1. **Cite the DID by view.** Every DID citation must state `accepted view` or `all-markup view` and give a `did_current.txt` / `did_all.txt` line number. "DID 1.5.1 says X" is not a citation while L-M11 stands.
2. **T10 and T40 are not VCU2 requirement sources.** They are VDU-derived (L-M12).
3. **Deleted text is evidence of a decision, not a requirement.** The `sys_rst1_n/2_n/3_n` deletion (§12.4) is reportable as an unexplained requirement removal; it is **not** authority for the resets existing.
4. **A vendor ticket can override a written spec, and here one does** (L-C12) — but a bug citing it must say so explicitly and must not present Allegro's clarification as an approved AMD requirement.
5. **Closed ≠ fixed for the target release** (L-C13). Release applicability remains unasserted pending Orion's `[SCOPE]`.
6. **Two dropped negations found (L-A28, L-A30).** Where a DID sentence's stated behavior contradicts its own surrounding logic, it is `SPEC_AMBIGUITY` — it is **not** an oracle, and it must not be read literally to manufacture a bug.

---

# 13. EOF / SYNC — RELEASE APPLICABILITY AND OWNERSHIP (Rev B addendum)

Retrieved serially from jira.xilinx.com after the Rev B §12.7 analysis. **This supersedes L-C13's "indeterminate duplicate" reading — the three tickets are not duplicates.**

## 13.1 The `sync_eof` family is a per-device propagation chain, not a duplicate set

| Key | Assignee | Status | Labels | What it actually is |
|---|---|---|---|---|
| **EDT-1094051** | Erusalagandi, Srikanth | **Closed** | `IMT_baseline_difference`, **`fixed_in_imt50`**, `security_review_closed`, `security_review_inprogress` | Origin ticket; verified on presilicon "with **T50 Fix**" |
| **EDT-1094076** | Maram, Shiva Reddy | **Closed** | *(none)* | The **implementation** ticket — PL workaround + P&R constraints; carries the CLs |
| **EDT-1096956** | Bromley, Gregg | **Assigned (OPEN)** | `IMT_baseline_difference`, `security_review_closed`, `security_review_inprogress` — **no `fixed_in_imt50`** | Propagation to a *different device* |

**The decisive sentence, verbatim, EDT-1096956 description (first line, above the copied body):**

> "**Making sure the FIX in T40 propagates to T3.**
>
> Gregg"

**L-C13R (replaces L-C13).** The three tickets share an identical copied body because the *defect statement* is identical; they differ in *device*. Corroboration: `fixed_in_imt50` is present on the T50-verified ticket and **absent** on the T3 propagation ticket — the label set is internally consistent with a per-device chain and is **not** evidence of a workflow contradiction on that axis.

### 13.1.1 Evidence tiers — what is *directly reported* vs *relayed by implication* (canonical)

Per the Sentinel Rev-B audit and Orion's `[CORRECTION]`, the earlier phrasing "T40 — fixed. T50 — fixed." was **stronger than its evidence** and is withdrawn. The corrected, tiered statement:

| Device | Tier | What the retrieved history actually shows |
|---|---|---|
| **T50** | **DIRECT, NARROW** | EDT-1094051 is Closed and carries `fixed_in_imt50`; the ticket text **reports presilicon verification "with T50 Fix" in a single 320x240 test**, plus encoder-frequency commentary. This is a *reported presilicon result on one narrow stimulus*, **not** a release sign-off, not a regression pass, not silicon evidence. |
| **T40** | **RELAYED BY IMPLICATION** | I hold **no ticket asserting T40 is fixed.** The only basis is the EDT-1096956 description line "Making sure the FIX in T40 propagates to T3," which *presupposes* a T40 fix. Existence of a T40 fix is **inferred from a propagation statement written for another purpose** and is weaker than the T50 tier. |
| **T3** | **DIRECT** | EDT-1096956 is **Assigned/OPEN**, reassigned toward the T3 PD lead. Directly reported open. |

**No release sign-off is inferred for any device.** "Closed" is a workflow state, not a verification verdict; `fixed_in_imt50` is a label, not a release gate. The fix/CL/netlist artifacts themselves remain **unopened** (§13.5), so no device row is corroborated by inspecting the change.

**Ruling: release applicability of this defect is device-dependent and only partially established from primary sources, at the tiers above.** I do not assert which device is in scope; Orion's `[SCOPE]` decides which row applies. The question "is EDT-1094051 being Closed evidence the target is fixed?" answers: **no — at most it is a reported narrow presilicon result, and only if the target is T50.**

## 13.2 L-C14 — the retrieved history *identifies* a netlist ECO plus a PL-side workaround plus software as the delivered remedy, and *exposes no VCU2-RTL-change evidence*

> **Scope of this section (Rev-B correction).** Everything below is what **retrieved Jira comments report**. I have **not opened** CL 6493847, CL 6493947, the ECO netlist, or any changed file, so I cannot state what the change contains or enumerate what it does and does not touch. Categorical forms — "the fix is not a VCU2 RTL change", "responsibility is on the PL side", "none modifies the VCU2 encoder" — are **withdrawn** and replaced by the reported/relayed forms used here.

**EDT-1096956, comment by Galagali, Abhay (the DID's own author of revision 1.5.1), verbatim:**

> "Hi [~gbromley] : I think this EDT should be assigned to T3 PD lead and not to me (Design team). **In any new device with VCU2, the implementation will be net-list based, and thus, PD should ensure that they pick the latest net-list (with ECO).** Can you please assign this EDT to the T3 PD lead?"

**EDT-1094076, comments by Bromley/Allagadapa/Maram, verbatim extracts:**

> "**PL workaround is implemented and tested**, but IP team is working on place and routing constraints for the PL logic"
>
> "The PL workaround has been implemented and tested with software changes, and the solution is functioning as expected. **Software changes are complete for both VCU2 ctrlsw/firmware and Gstreamer.** Pending items: Work on place and route constraints for the PL logic"
>
> "For **WA#1** we have implemented pblock and tested. We have also tested **WA#2** and Yash also tested and confirmed it is Working. We have ensured Timing is also clean. … Once confirmed, we will push WA#2 to release"
>
> "**WA2 applied Design changes pushed to builds with below CL — HEAD (CL 6493847) and REL (CL 6493947)**"

**Bearing on Orion's D2/U2 question "who must regenerate exactly-one-encoder-clock EOF". This weakens the RTL-difference hypothesis; it does not refute it by inspection:**

1. **Every remedy named in the retrieved history sits in the PL Frame-Buffer-Write datapath.** A **PL workaround** (two variants, WA#1 pblock-based and WA#2 shipped), plus CR-1271467 ("Add flop for `sync_eof` **inside FBW**") and ER-28337 ("update **FB_WR IP** to force 1 extra EOF signal"). **No retrieved comment describes a VCU2 encoder change** — which is *absence of evidence in the comment stream*, not a verified inventory of the change. Whether pulse-shaping *responsibility* is contractually on the PL side is **not stated in any requirements document** (see (d) below).
2. **The design author states the silicon implementation path is netlist-based.** Verbatim: implementation is *net-list based*; PD must "pick the latest net-list (with ECO)". **If that relayed premise holds, the supplied RTL shadow tree can legitimately lack this fix and still be the correct RTL — making absence-from-RTL non-probative.** I have not opened the netlist or ECO to confirm. Consistent with the standing ruling that the implemented netlist, not the RTL, is the artifact of record for what a device does.
3. **The reported remedy is not purely hardware.** Software changes in **VCU2 ctrlsw/firmware and GStreamer** are reported as part of the delivered solution — so this requirement carries a **software precondition**, which belongs in the requirement record and appears in no requirements document.

**Direct challenge to Tracer's hypothesis (per my challenge mandate).** Tracer reports that the supplied source closure feeds a multi-cycle synchronized level into a readable level-sensitive consumer with no downstream edge detect. That observation may be accurate about the supplied tree and **still not constitute a VCU2 bug**, on four independent grounds:
- (a) **Authority** — the "exactly one cycle" requirement originates in an **Allegro vendor clarification**, not in an approved AMD requirements document. The AMD-side written spec quoted in the ticket says "**at least one** clock cycle", under which a multi-cycle level is **compliant** (L-C12).
- (b) **Release** — fixed in T40/T50; open only for T3. Applicability is unset until `[SCOPE]`.
- (c) **Artifact** — the delivered remedy is *reported* as a **netlist ECO** plus a **PL-side workaround**. **If that relayed premise holds**, absence from the RTL shadow tree is expected rather than anomalous, and checking RTL for this fix tests the wrong artifact. The premise is relayed, not verified: the CLs and netlist are unopened.
- (d) **Locus** — every remedy named in the retrieved history sits in the **PL DMA/FBW producer** rather than the VCU consumer. A finding phrased as "the VCU lacks an edge detect" attributes the defect to a block that no retrieved remedy touches, unless a source is produced requiring the VCU to edge-detect. **No such source exists in the DID, in Arch v1.2, or in any of the three EDTs.** `SPEC_AMBIGUITY` — the DID and arch spec never state which side owns pulse shaping.

**Changelists (first RTL/build identifiers recovered in this investigation): HEAD `CL 6493847`, REL `CL 6493947`.** These are Perforce CLs for the **WA#2 PL design change**, not for VCU2 RTL. `p4` is not on PATH in this session, so I cannot open them. **BLOCKED — CL numbers published for a peer with Perforce access.**

## 13.3 `ONE_INSTANCE` and `LLP_MODULE` — BOUNDED NEGATIVE (searched supplied documents only)

> **Bound (Rev-B correction).** This is a **bounded negative in the complete tracked-change-aware text of the supplied DID 1.5.1 and Arch v1.2** — not an authoritative negative about the parameters. **Neither document is established as governing the target device** (release applicability is UNSPECIFIED pending `[SCOPE]`; the DID is VDU-derived), and the sources that would normally define IP parameters — packaged IP / `component.xml` / xgui Tcl / a Product Guide — are **omitted and blocked** (§13.5). Absence here bounds *these two documents*, nothing wider.

Searched the complete tracked-change-aware text of both primary documents:

```
grep -c -iE 'one_instance|llp_module'  did_all.txt   -> 0
grep -c -iE 'one_instance|llp_module'  arch_spec.txt -> 0
```

**Neither identifier occurs anywhere in the DID (1.5.1, all-markup view, including deleted text) or in the Everest MMD VCU Architecture Specs v1.2.** Not as a parameter, not as a generate condition, not as a configuration name, not in any table. **This is exhaustive for these two files and only for these two files.**

**L-T12 / SPEC_AMBIGUITY.** `ONE_INSTANCE` and `LLP_MODULE` are **RTL/IP-packaging parameters with no requirements basis in either supplied document.** Within that bound:
- **No authoritative definition** of either parameter exists in my sources.
- **No supported-configuration list**, no legal combinations, no defaults.
- **No SKU/device mapping** — nothing ties either parameter to T20/T40/T50/T3.
- **No statement of responsibility for EOF pulse shaping** conditioned on them.

**Per my mandate I will not infer values, defaults or applicability from parameter names.** Sentinel's instruction to keep target applicability **BLOCKED absent an authoritative setting is correct and I concur — applicability stays blocked on this axis.**

**What the documents *do* say about the mode these parameters plausibly gate**, so the negative result is not mistaken for total silence (Arch v1.2, §1.11.4.1 "Ultra-Low Latency mode", p.13, verbatim):

> "In this mode, each of the 2 encoder cores within one encoder instance is used to encode a separate 4Kp30 stream, and both cores share the L2 cache. However, the L2 cache is sized for the normal case of both encoder cores sharing a single 4Kx2K stream, each encoding one half. Therefore, the L2 cache is under-sized for this case, and 2 mitigation choices are available"

and §1.5 (p.5): "The encoder **shall** support 2 concurrent low latency streams up to 4Kp30 each, one per [core]" — with "Support for **slice-based ultra-low latency mode via PL interface**" listed as a top-level feature (DID `did_current.txt:104`, Arch `:203`). **This is the only normative ULL requirement I have found, and it is about stream count and cache sizing — it says nothing about pulse shaping, EOF ownership, or instance/module parameterization.**

## 13.4 L-C15 — encoder clock frequency: 950 MHz is asserted in Jira and unsupported by the architecture spec

The EOF requirement is expressed in **encoder clock cycles**, so the clock rate is load-bearing. All three EDTs state "**The encoder clock is 950 MHz**". Arch v1.2 §1.13 "Expected Required Clock Speeds" gives **minimum** encoder rates per format (UHD 4Kp60, MHz): AVC 4:2:0 540 / 4:2:2 550 / 4:4:4 785; HEVC 4:2:0 560 / 4:2:2 615 / **4:4:4 890** — with the note that "actual speed targets are quoted previously and are **7% higher, for DCI support**" (890 × 1.07 ≈ 952).

So **950 MHz is consistent with, and evidently derived from, 890 + 7%** — but the architecture spec **never states 950 MHz as the encoder clock**; it states a family of per-format *minima* plus a percentage. **The single number on which the EOF timing requirement depends is nowhere stated as a requirement.** `SPEC_AMBIGUITY` — usable as an engineering figure, not citable as a specified value.

## 13.5 IP packaging metadata — access blocker, recorded exactly

Orion reports the supplied tree lacks VCU2 `component.xml` / xgui Tcl / IP packaging, so `ONE_INSTANCE`/`LLP_MODULE` visibility, defaults, legal combinations and SKU applicability cannot be determined from it. **I confirm I cannot supply that metadata either, and I record the blockers precisely rather than substituting a weaker source:**

| Candidate authority | Status |
|---|---|
| DID 1.5.1, Arch v1.2 | **Searched, 0 hits** (§13.3). Definitive negative. |
| VCU2 packaged IP / `component.xml` / xgui Tcl | **Not present in any path I can read.** Not in `/everest/apex_pvs_nobkup/karthick/vcu_analysis`. |
| Product Guide (PG-series) for VCU2 | **Not referenced in T03 and not supplied.** No PG number appears in either document. |
| SharePoint Encoder/Decoder Spec folders (T03 r5/r6) | **BLOCKED** — interactive auth required; targets are *folders* with no filename or revision (L-T06R). URLs published in §12.3. |
| `VCU2_Address_Map.xlsx` | **Not supplied, not referenced in T03** (L-T10). |
| Allegro tickets 5011 / 6213 | **NOT ATTEMPTED** — third-party vendor tracker; no authorization to authenticate to an external vendor system. Requires explicit user authorization. |
| Perforce CLs 6493847 / 6493947 | **BLOCKED** — `p4` not on PATH in this session. |
| Jira `fixVersions` / `versions` / `issuelinks` / attachments | **UNAVAILABLE via this connector** (§12.8) — absent from the payload, *not* empty in Jira. |
| EDT-1094051 attachment `image-2025-12-15-18-09-12-922.png` | **Cannot retrieve or view.** No waveform content is attributed to me. |

**Nothing above is reported as "the spec is silent" where the truth is "I could not open it".** The two categories are kept separate: §13.3 is a *searched* negative on documents I hold; this table is a list of documents I do *not* hold.

---

# 14. EVIDENCE PROVENANCE TIERS (canonical, applies to the whole report)

Adopted after the `vcu-sentinel` Rev-B audit and the `vcu-orchestrator` `[CORRECTION]`. **Any claim in this report may be read at no higher tier than the one marked here.** Where an earlier section stated a claim more strongly than its tier permits, the tiered form in this section governs.

| Tier | Meaning | Admissible phrasing |
|---|---|---|
| **T-A — PRIMARY, INSPECTED** | I opened the artifact and read the text myself | "the document states", "verbatim" |
| **T-B — DIRECTLY REPORTED** | A retrieved primary record (Jira field/comment) asserts it in its own words, about itself | "EDT-x reports…", "the ticket is Assigned/OPEN" |
| **T-C — RELAYED BY IMPLICATION** | Presupposed by a retrieved record written for another purpose; no record asserts it directly | "relayed by implication", "presupposed by" |
| **T-D — BOUNDED NEGATIVE** | Exhaustive search of artifacts I hold; says nothing about artifacts I do not hold | "not present in the searched supplied documents" |
| **T-E — BLOCKED** | Could not open. **Never** to be reported as spec silence | "I could not open X" |

**Tier assignments for the load-bearing claims:**

| Claim | Tier |
|---|---|
| DID 1.5.1 / Arch v1.2 quoted text, tables, tracked changes | **T-A** |
| Pre-config CDO + startup register sequence (§15) | **T-A** |
| T3 `sync_eof` is open/Assigned | **T-B** |
| T50 presilicon fixed (one 320x240 test) | **T-B, narrow** — reported result, **not** release sign-off |
| T40 is fixed | **T-C** — inferred from the T3 propagation sentence only |
| Remedy = PL workaround + netlist ECO + ctrlsw/firmware/GStreamer changes | **T-B** — reported; artifacts unopened |
| "No VCU2 RTL change" | **T-C at best** — absence of evidence in the comment stream, not a verified inventory |
| Encoder clock = 950 MHz | **T-C** — Jira-asserted; Arch v1.2 gives per-format minima + "7% higher", never 950 (L-C15) |
| `ONE_INSTANCE` / `LLP_MODULE` absent | **T-D** — bounded to DID 1.5.1 + Arch v1.2 |
| Packaged IP, PG, SharePoint specs, Allegro tickets, CLs, MMD general arch spec | **T-E** |

**Standing rule (L-R-TIER).** No release sign-off, no verification verdict, and no implementation fact may be derived from a Jira workflow state or label. `Closed` and `fixed_in_imt50` are process metadata at **T-B about the process**, and carry **no tier at all** about silicon behavior.

**XRDB (correction to §5).** `http://zynq_design:8070` returns **HTTP 200 on an unauthenticated root GET**. That is **reachability only.** The XRDB MCP is down, **no query path has been found, and zero register data has been retrieved.** HTTP 200 **does not mitigate** the MCP/query/data gap — §5 remains **`XRDB-UNAVAILABLE` / T-E**, and every mandated XRDB search remains unexecuted.

**Scope note on peer contradiction CONTR-001.** From my side it may be closed **only** as *"no DID cell or requirement-ID misassociation was found in the tracked-change-aware text of the supplied DID"* — a **T-D bounded negative about document association**. It is **not** implementation proof and must not be recorded as one.

---

# 15. PRE-CONFIG CDO AND STARTUP SEQUENCE — the first register-level normative sequence recovered (T-A)

Recovered from `did_all.txt:1400-1500` (tracked-change-aware view of DID 1.5.1). **These are the only XRDB-shaped hierarchy paths found anywhere in either supplied document**, and they are the concrete targets for the XRDB traceability work that §5 has been unable to perform.

## 15.1 L-R70 — VCU2 pre-config CDO (executed by PLM)

Ordering is normative and the steps are sequential:

| # | Step | Register / value (verbatim) |
|---|---|---|
| 1 | Poll power supply | `VCU2_NPI_0.MMD_NPI_PCSR_STATUS.MMD_PWR_SUPPLY == 1` |
| 2 | Unlock | Write `32'hF9E8D7C6` to `VCU2_NPI_0.NPI_PCSR_LOCK` |
| 3 | Release IPOR | `MMD_NPI_PCSR_MASK.MMD_IPOR = 1` then `MMD_NPI_PCSR_CONTROL.MMD_IPOR = 0` |
| 4 | **EDT-1066811 ECO** | After IPOR released, enable functional clock: `VCU2_NPI_0.VCU2_NPI_ECO_REG_2 [2:0] = 3'b010` |
| 5 | Poll eFuse | `MMD_NPI_PCSR_STATUS.SSC_EFUSE_VALID_STATUS == 1` — **else flag error and halt** |
| 6 | DFX isolation off | MASK/CONTROL `ctrl_iso_0_mmd_pl_dfx` = 1/0 ; `ctrl_iso_4_mmd_noc_dfx` = 1/0 |
| 7 | Scan clear | MASK/CONTROL `SCAN_CLEAR_TRIGGER_<3:0>` = `0xf`/`0xf` ; **wait 20 µs** |
| 8 | Verify scan clear | `SCAN_CLEAR_DONE_<3:0> == 0xf` and `SCAN_CLEAR_PASS_<3:0> == 0xf` — **else halt and report** |
| 9 | BISR | Transfer PMC BISR eFuse cache → VCU2 BISR Fuse Cache register; **"starting offset is 0x104 – verify in xregdb"**; MASK/CONTROL `BISR_TRIGGER` = 1/1 ; poll `BISR_DONE==1` and `BISR_PASS==1` |
| 10 | **EDT-1066811 ECO** | Before mem-clear, switch to NPI clock: `VCU2_NPI_ECO_REG_2` `[1]=1'b0`, `[2]=1'b1`, `[0]=1'b1` |
| 11 | Memory clear | MASK/CONTROL `MEM_CLEAR_TRIGGER_<3:0>` = `0xf`/`0xf` ; **"Wait [Need value] µs"** ; verify `MEM_CLEAR_DONE_<3:0> = 0xf` and `MEM_CLEAR_PASS_<3:0> = 0xf` |
| 12 | **EDT-1066811 ECO** | After mem-clear, switch back: `[0]=1'b0`, `[2]=1'b0`, `[1]=1'b1` |

**Programming model (L-R71, normative).** Every control action is a **mask-then-control write pair**: `MMD_NPI_PCSR_MASK` must be written `1` for a bit **before** the corresponding `MMD_NPI_PCSR_CONTROL` write takes effect. A control write without the preceding mask write is a **prohibited/no-effect operation**.

**Required error behavior (L-R72).** "Flag error and halt process" is specified at three points: eFuse-valid poll failure, scan-clear DONE/PASS mismatch, BISR DONE/PASS mismatch. This is the **only explicit required error behavior found in either supplied document.**

## 15.2 L-R73 — startup sequence (WDI-generated `rnpi` CDO)

Precondition, verbatim: executed **"after PL and NoC configured and operational"**.

| # | Step |
|---|---|
| 1 | MASK `ctrl_iso_1_mmd_pl`=1, `ctrl_iso_3_mmd_noc`=1, `ctrl_iso_2_mmd_ram`=1 |
| 2 | CONTROL `ctrl_iso_1_mmd_pl`=0, `ctrl_iso_3_mmd_noc`=0, `ctrl_iso_2_mmd_ram`=0 |
| 3 | MASK `INITSTATE`=1 ; CONTROL `INITSTATE`=0 |
| 4 | MASK `PCOMPLETE`=1 ; CONTROL `PCOMPLETE`=0 |

Closing statement, verbatim: **"Further required operations are handled by the VCU driver through NoC."** — i.e. the DID's normative coverage **terminates** at PCOMPLETE; everything past it is a documented handoff to software with **no requirements text in either supplied document** (`SPEC_AMBIGUITY`, L-A33).

## 15.3 Defects exposed by this sequence (new)

| ID | Defect | Class |
|---|---|---|
| **L-A34** | **"Wait [Need value] µs"** — an **unresolved TBD inside a normative timed sequence.** The memory-clear settling time is unspecified, so the sequence is **not executable as written** and no verification can bound it. | `SPEC_AMBIGUITY`, high severity |
| **L-A35** | **"starting offset is 0x104 – verify in xregdb"** — the BISR cache offset is **self-declared unverified** by the document. A normative address carries an instruction to check it elsewhere; XRDB is **unavailable (§5/T-E)**, so it **cannot** be checked. | `SPEC_AMBIGUITY` + traceability gap |
| **L-C16** | **Assert-vs-write-0 contradiction:** the step prose says **"Assert PCOMPLETE"** while the register action writes `CONTROL.PCOMPLETE = 0`. Either the signal is active-low or the prose is wrong; **the document does not say which.** The same pattern appears for `INITSTATE`. Polarity of these two bits is **undetermined from the DID alone.** | **Conflict**, high risk — an implementer following prose and an implementer following the register writes produce **opposite** behavior |
| **L-A36** | Verbatim open question in the document: **"Need to confirm this with S/W team"** regarding whether eFuse-valid polling may run concurrently. Concurrency of step 5 is **undecided in the released text**. | `SPEC_AMBIGUITY` |
| **L-T13** | `EDT-1066811` clock-switch ECO steps are woven into the normative sequence, so the **published sequence is already ECO-dependent**. Retrieval of EDT-1066811 is pending (Jira rate-limited). Until then the ECO's applicability **per device is unknown** — and per L-C14 the silicon path is netlist/ECO-based, so this matters for release applicability. | Traceability gap |

## 15.4 Deleted reset table (T-A, tracked-change recovery)

`{DEL:Table 9 11 VDU Resets}` — table **T32, deleted** in the tracked-change record:

| Reset | Deleted description (verbatim) |
|---|---|
| `pl_vcu_raw_rst_n` | "Reset from PL… **Doesn't reset DFX, SSC and BISR.** Resets VCU_INT_WRAP" |
| `por_pl_b` | "Active LOW POR reset from PMC… through POR IP and NPI IPOR register… **Resets whole block**" |
| `sys_rst1_n` | "Soft reset from PMC \| **TBD**" |

**L-A37.** The **only table that documented reset scope and what each reset does/does not clear was deleted**, and one of its three rows was already `TBD` when deleted. The surviving text specifies no replacement. **Reset-scope semantics for VCU2 are therefore unspecified in the current DID** — while §15.1 depends on IPOR behavior. This is the reset-side counterpart to L-A38 below.

---

# 16. RESPONSE TO ORION — EDT-1098410 / MMD ISOLATION AUTHORITY

**Question:** locate the authoritative MMD general-architecture isolation/clamp document and the original passage defining register-content retention / reset behavior or AXI quiesce around `ctrl_iso_1_mmd_pl`. Instruction honored: **"Do not infer register semantics from signal clamp."**

**Finding: the authority chain terminates in a document that is not identified well enough to fetch.** Arch v1.2 **§1.30.3 "Isolation, Clamp, and Level Shifters ('ICLS')", verbatim and complete:**

> "The VCU requires isolation, clamp and level shifters (ICLS). ICLS is required because the VCU has its own dedicated power domain. **See MMD tile general arch specs.**"

That is the **entire** section — three sentences, ending in a deferral. The same deferral recurs at Arch §1.30.1 ("See MMD tile general arch specs. TBD even/odd half-FSR row limitations."), at line 134 ("For general details regarding MMD technology refer to the Everest MMD general architecture specifications document"), and at line 332 ("VCU is a 2x tall MMD tile (replacing 2 GT quads). See MMD general arch specs for details.").

**The deferral target, Arch v1.2 §1.36 References, verbatim and complete:**

> `[1] Diablo VCU PDD` · `[2] Everest VDU architecture specs` · `[3] Everest MMD general architecture specs` · `[4] Telluride top level architecture specs`

**No revision, no URL, no document number, no date for any of the four.** `[3]` is the sole authority for ICLS and **is not in my possession, not in `/everest/apex_pvs_nobkup/karthick/vcu_analysis`, and not resolvable from the citation as written.**

**L-A38 / `SPEC_AMBIGUITY` + L-T14 / stale-or-unresolvable document link (blocker, stated exactly).**
- **Register-content retention across isolation: NOT SPECIFIED** in any document I hold.
- **AXI quiesce requirement around `ctrl_iso_1_mmd_pl`: NOT SPECIFIED** in any document I hold.
- The **only** normative material I can attribute to `ctrl_iso_1_mmd_pl` is its **position in the startup ordering** (§15.2): it is de-isolated **after PL and NoC are "configured and operational"** and **before** `INITSTATE` and `PCOMPLETE`, via a mask-then-control pair. **That is an ordering constraint, not a semantic one.**
- **I decline to infer retention or quiesce semantics from the ICLS clamp description, per your instruction and per my mandate not to invent expected behavior.**

**Exact blocker for escalation:** obtain **"Everest MMD general architecture specs"** — cited as reference `[3]` of *Everest MMD VCU Architecture Specs v1.2*, **with no revision, URL or document number given**. Until a revision-identified copy is produced, EDT-1098410's expected-behavior question **has no authoritative answer available to me (T-E, not spec silence).**

---

# 17. WAIVER FORENSICS — EDT-1066841 AND XPS/603256108 (assigned by Orion + Sentinel)

**Access note (changed this session).** The `cloud_atlassian` Jira connector now returns **HTTP 401 `Invalid user Jira token … Unauthorized`** on every call — a **hard credential failure**, not the earlier 429 rate-limit. All Jira results below were retrieved through the **`sage` Xilinx-Jira connector**, which still authenticates but **truncates long comments and does not return attachments, issuelinks, or fixVersions.** Where a comment is cut off I mark it and do not guess the remainder.

## 17.1 EDT-1066841 "T50 : VCU isolation waiver" — T-A/T-B, and it is *not* purely physical

| Field | Value |
|---|---|
| Status | **Closed** |
| Assignee | **Galagali, Abhay** (the DID 1.5.1 author) |
| Reporter/originator (from text) | **Patel, Hemang Jayprakash** ("Hemang") |
| Created / Updated | **2024-01-09** / **2024-05-03** |
| Labels | **`FCV`, `T50`, `non_rtl_issue`** |
| Attachments | **Not returned by this connector** — an email thread is referenced in a comment but is **unretrieved (T-E)** |

**Description, verbatim:** *"I am seeing isolation clamp and reset miss-match for attached signals. Kindly review and approve wavier."*

**Comment 1 — Galagali, Abhay, verbatim (TRUNCATED by connector):**
> "Summarizing our discussion here:
> * Regarding **`if_npi_regs_status.mmd_npi_pcsr_control[6]`**: **The latest UPF does not include this pin in ISO strategy** and it looks like your results are not current. Please get the latest run data and check if this is still present.
> * Regarding sign…" **[TRUNCATED — remainder not retrieved]**

**Comment 2 — Galagali, Abhay, verbatim:**
> "As per Shreel's response, we can waive **`noc_vcu2_scan_chnl_in [11:0]`**. As per Lizhi response, we can waive **NOC NMU and NSU inputs to VCU2**. **Email thread attached.** Please re-run with these waiver and share results."

**Comments 3–5 — Patel, Hemang Jayprakash:** *"Waiver approved."* / *"Testcase passed with waiver."* / *"Waiver approved."*

### 17.2 Answer to Sentinel's question: physical-only, or functional reset/isolation implications?

**Split verdict — the two artifacts are not the same kind of thing.**

| Artifact | Character | Verdict |
|---|---|---|
| **XPS/603256108 + EDT-1094741 (+EDT-1073792)** | **EMIR / IR-drop / scan physical signoff** | **Purely physical** — see §17.3 |
| **EDT-1066841** | **Isolation-clamp and reset mismatch, `FCV`-labelled** | **NOT purely physical.** It concerns **isolation and reset connectivity on named signals**, including a **PCSR control bit** — the same register family that §15 shows is normative for the boot sequence |

**L-W01 (waiver record, EDT-1066841).**
- **Affected scope (waived):** `noc_vcu2_scan_chnl_in[11:0]`; **NOC NMU and NSU inputs to VCU2**. Both are **interface-boundary** signals — NoC ingress and scan channel.
- **Separately dispositioned, NOT waived:** `if_npi_regs_status.mmd_npi_pcsr_control[6]` — dispositioned as **"the latest UPF does not include this pin in ISO strategy"** and **"your results are not current"**, i.e. **closed as a stale-data artifact, not as an accepted deviation.**
- **Approving authority:** the waiver rationale is authored by **Galagali, Abhay**, sourced to **"Shreel's response"** and **"Lizhi response"** relayed in an **attached email thread I cannot open**. The literal "Waiver approved" strings are written by the **reporter**, Patel. **So the approval of record is relayed through a summarizer and grounded in an unretrieved email — approval authority is documented by assertion, not by an inspected approval artifact.**
- **Conditions / expiry:** **NONE STATED.** No corner, no mode, no device-revision bound, no expiration, no re-review trigger. The only condition present is procedural ("re-run with these waivers and share results").
- **Release applicability:** label **`T50`** only. **Nothing extends this waiver to T3, T20 or T40** — and per §14 no applicability may be inferred from its absence.
- **Verification linkage:** *"Testcase passed with waiver"* — **the test is not named, its content is not described, and no result artifact is attached.** Unverifiable as written.

### 17.3 XPS/603256108 "VCU2 EMIR checklist and Release details" (T-A, page version 50)

Purely **electromigration / IR-drop physical signoff.** Contents of record:

- **Signoff run matrix** — `vcu2_core`: Static, all_vless, decap2pad, tap2pad, FIT = Yes; **scan vless / scan_shift / scan_capture = "Yes (excluding decoder & encoder)"**; `vcu2_enc_top` and `vcu2_dec_top` cover the scan modes separately.
- **Waiver of record:** `vcu2_core`, run types **Thermal vless** and **scan capture**, **1 IR (84.5 mV)** and **1 IR (85.3 mV)** → **EDT-1073792**.
- **Review EDTs:** EDT-1069571 (all three blocks), EDT-1094929, EDT-1069699. **Waiver EDTs:** EDT-1073792, EDT-1094741. **ECOs:** `vcu2_core` ECO_10, `vcu2_enc_core_top` ECO_9, `vcu2_dec_top` ECO_11 (plus a `14-ECO` comparison run).
- **Release pointers** to `/proj/release/release/everest/v1/vcu2_v1_6t0_block/…` DEF/SPEF/TWF/ROM snapshots; one **TWF path is under a `T40` altrelease branch** while the DEF/SPEF are under `FCP23`/`FCP28`.

**EDT-1094741 "[VCU2] IR waiver in low latency encoder fix db"** — Closed, approved by **Bhonge, Shashank** with the entire rationale being *"Approving waiver, Thanks, Shashank"*. Waived items are **two physical cells**: a `BUFFSKRD18…` at **89.1 mV** and a `DCCKBEMMVPD18…` at **84.1 mV**, both **Thermal vless**, both inside `…/i_vcu2_iso_wrap/i_vcu2_int_wrap/g_core_num_{0,1}_.i_vcu2_enc_core_top/i_ENC_alg_core_wrapper`, with positive setup/hold slack (43/28 ps and 43/132 ps).

**L-W02.** These are **IR-drop magnitude exceptions on individual cells, with no functional-behavior content.** They do not alter any requirement, register, reset or isolation semantic. **Sentinel's "purely EMIR/scan/DFX physical-signoff exception" characterization is correct for XPS/603256108, EDT-1094741 and EDT-1073792.**

**L-W03 — but one fact in EDT-1094741 is materially relevant to §13.2.** Its description states verbatim: *"vcu2_core with the latest **eol and eof fix db** observing below IR violation"*. This is an **independent ticket, in a different discipline, written by different people, that presupposes the existence of an EOF-fix design database for `vcu2_core`.** It **strengthens** the relayed premise in §13.2 that an EOF fix exists as a **physical/netlist database** rather than only as a PL-side change — the fix db was carried far enough to be **IR-signed-off at the `vcu2_enc_core_top` level** (the waived cells sit **inside the encoder core**). **Tier: still T-C** — I have not opened the db and the ticket does not describe the change's content — but the corroboration is **independent**, which is stronger than the single-source relay I recorded before. **It also cuts against the categorical form of "the remedy touches nothing inside the VCU2 encoder": an EOF-fix database evidently exists whose IR signoff is taken at the encoder core.**

**L-W04 / traceability + governance gaps in the waiver set:**
| Gap | Detail |
|---|---|
| **No expiry or conditions on any waiver** | Neither EDT-1066841, EDT-1094741 nor EDT-1073792 states a corner, mode bound, device-revision bound, expiry or re-review trigger. |
| **Approval is one line** | EDT-1094741's entire approval rationale is *"Approving waiver"*. No criteria, no threshold cited, no limit referenced. **No signoff criterion document is linked from the page.** |
| **Approval grounded in unretrieved email** | EDT-1066841's substantive rationale lives in an **attached email thread** (T-E). |
| **Page carries no dates or approver** | XPS/603256108 returns **empty `created`/`updated`**, is at **version 50**, has **three "Date :" headings that are literally blank**, and **names no page owner or approver.** A signoff page whose dates are empty cannot establish *when* a release pointer was valid. |
| **Applicability** | EDT-1066841 = `T50` only. XPS/603256108 = `everest/v1/vcu2_v1_6t0_block`, with one TWF under `T40`. **No mapping to T3/T20 anywhere.** |
| **`non_rtl_issue` label** | EDT-1066841 is explicitly labelled `non_rtl_issue`, consistent with L-C14's netlist/physical locus — and consistent with the standing ruling that the **implemented netlist is the artifact of record**. |

### 17.4 Direct bearing on EDT-1098410 (§16)

EDT-1066841 is the **closest thing to an isolation authority I have found** — and it **still does not answer §16.** It disposes of **specific pins** against a **UPF ISO strategy**; it **never states register-content retention behavior or an AXI quiesce requirement**, and it cites **UPF as the governing artifact**, not a requirements document. Notably `if_npi_regs_status.mmd_npi_pcsr_control[6]` was dispositioned as **excluded from the ISO strategy in the latest UPF** — which is a **statement about the power-intent file, not about required behavior.**

**L-A39 / ruling.** Per Orion's instruction *"do not infer register semantics from signal clamp"* — **I decline to derive PCSR retention semantics from this waiver.** The correct reading is: **the governing artifact for VCU2 isolation is the UPF, and the requirements documents I hold are silent.** To close EDT-1098410 someone must produce (a) the **VCU2 UPF revision** in force for the target device, and (b) the **"Everest MMD general architecture specs"** (§16, reference `[3]`, no revision/URL/number). **Both are T-E for me.**
