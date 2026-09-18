# PROVER — Verdict Ledger (`proof-gate`)

Independent falsification and sign-off record for candidate VCU silicon bugs.
Operating mode: **read-only**. No subagents. Consensus and agent confidence are not evidence.

**Status: PHASE 0 (source qualification) CLOSED 2026-08-12. No candidates received. No verdicts issued.**

Phase-0 closure terms (sentinel [SENTINEL-ACK], orchestrator [STATUS], both 2026-08-12): SENT-005 closed after direct re-verification of this file. This deliverable is accepted **as a nonterminal source/evidence ledger only** — it qualifies sources, it does not adjudicate anything. No candidates, no verdicts, no execution. Blockers D1, D2, D7, D8, D9 and all others remain **open**; closure of SENT-005 does not close any of them.

Last updated: 2026-08-12

---

## 0. Standing rules of evidence for this audit

1. Every candidate is reconstructed from primary sources. RAG hits are **leads**, never proof.
2. No claim that a simulation, formal run, compile, or test "passed" unless it was actually executed in this session. Nothing has been executed. Nothing has been compiled.
3. A verdict requires the full 17-point chain (requirement authority → RTL reachability → trigger legality → cycle reasoning → observability → history/waiver) plus at least one proof-artifact proposal.
4. Exactly one verdict from the fixed enum per candidate.
5. Where the chain cannot be closed, the output is a **documented evidence blocker**, not a softened verdict.
6. **Canonical-copy rule (SENT-005).** `/scratch/karthick/fonely/vcu_audit/prover_verdicts.md` — this file — is the **sole authoritative deliverable**. The earlier working copy at `/scratch/karthick/sage/production/vcu_audit/prover_verdicts.md` is **abandoned as of 2026-08-12**: it is not edited, not synced, and must not be cited by any agent. Any divergence between the two is resolved in favour of this file without inspection of the other. This closes the canonical-drift risk sentinel raised; it is a one-way abandonment, not a sync.

---

## 1. Block-family naming — PARTIALLY RESOLVED / APPLICABILITY BLOCKED

**Correction history (SENT-005, accepted in full).** This section has been corrected twice for overstatement, both times on peer challenge, both times correctly. Draft 1 was headed "Product / release identity — RESOLVED". Draft 2 still asserted the three names are "the same device". **Both overstated the evidence.** What the architecture document establishes is *terminology*: that the name "MMD VCU" denotes the Telluride-family block also called VCU2. It does **not** establish that the supplied RTL snapshot is the applicable implementation, nor that these documents govern the user's target.

Precisely what is and is not established:

| Claim | Status |
|---|---|
| The arch document *names* MMD VCU as Telluride-family / VCU2, distinct from Diablo ZU+ VCU | **Established** (P1, direct quote) |
| Therefore the names "Everest MMD VCU", "Telluride VCU" and "VCU2" refer to one block family | **Established** — naming only |
| Therefore `rtl/vcu2_v1_7t` as supplied here is the implementation those documents govern | **NOT established — blocked.** Requires independent RTL metadata / config / P4 applicability evidence (D1). The tree is mutable and unpinned. |
| Therefore these documents state the requirements for the user's target product/SKU/stepping | **NOT established — blocked** (D2, D7) |

Binding the supplied RTL to these documents requires independent applicability evidence that does not exist yet. Until D1 and D2 close, family naming must not be laundered into release applicability.

The mapping below is derived from the document itself rather than from RAG.

**Everest_MMD_VCU_Arch_Specs_1.2 (1).pdf**, body text:

> "The MMD VCU is part of the Telluride family of products. The MMD VCU is sometimes referred to as **VCU2**, to distinguish it from the original **Diablo (Zynq Ultrascale+) VCU**."

> "It shall be designed to fit within the footprint of two GTY quad modules and can replace GTY quad pairs along the right edge of an **Everest (Telluride) SoC** device." Target process: **7nm**.

Both excerpts: **arch spec 1.2, page 4, §1.1 Introduction** (23-page document). Read directly from the PDF.

**Consequence — the one rule this does support.** Because the architecture document explicitly distinguishes VCU2 from the original Diablo (ZU+) VCU, any candidate that imports a requirement or behavior from **Diablo / Zynq UltraScale+ VCU** is citing a *different, explicitly distinguished* product, and is rejected on requirement authority (checklist item 2) unless it independently re-derives the requirement for VCU2. This rejection ground depends only on the naming distinction, which *is* established, so it survives the applicability block.

**What this does NOT license:** it does not license treating a requirement in these documents as binding on the supplied RTL snapshot or on the user's target. Cross-citation between arch spec, DID and `rtl/vcu2_v1_7t` is a *working hypothesis* for locating evidence, **not** an established applicability chain.

Arch spec 1.2 enumerates deltas vs Diablo VCU: improved encode quality, re-architected cores, 2 encoder cores per encoder instance, frame buffer compression, hardened encoder 'L2 cache', interlaced support, 4:4:4 color format. **Diablo-derived behaviour must not be imported into VCU2 reasoning without independent VCU2 authority.** In these seven areas the architecture explicitly changed, so a Diablo-sourced assumption is *inapplicable unless re-derived for VCU2* — this is a re-derivation obligation, not a presumption that every such assumption is false.

### 1.1 Third-party IP boundary — standing classification rule

Arch spec 1.2: *"The following are the chosen Encoder and Decoder IP configurations provided by **Allegro DVT**."*

The encoder and decoder cores are **third-party hard IP**, not AMD-authored RTL. This materially changes disposition. For any candidate landing inside Allegro IP, I will require the reconstruction to state explicitly whether the defect is in (a) Allegro IP internals, (b) AMD integration/wrapper logic, or (c) the configuration selected. These carry different owners and different remedies, and conflating them has historically produced misfiled defects. This is a classification constraint I will enforce, not a preference.

---

## 2. Document revision qualification — TWO MATERIAL HAZARDS FOUND

### 2.1 Docs RAG is STALE against the supplied architecture spec — HIGH IMPACT

On-disk **supplied candidate primary source**: arch spec **v1.2, 6/20/24, author Ygal**. Its authority and applicability are **unresolved under D2/D7** — I do not call it authoritative anywhere in this document, and neither should any candidate citing it.

Full revision history extracted from the PDF:

| Version | Date | Author |
|---|---|---|
| 0.4 | 3/1/21 | Ygal |
| 0.5 | 4/30/21 | Ygal |
| 0.7 | 6/30/21 | Ygal |
| 1.0 | 10/1/21 | Ygal |
| 1.1 | 1/11/23 | Ygal |
| **1.2** | **6/20/24** | **Ygal** |

`sage.search_docs` indexes **only `Everest_MMD_VCU_Arch_Specs_1.0.pdf` and `..._1.1.pdf`. Version 1.2 is absent from the RAG corpus.**

**Consequence:** any candidate whose requirement was sourced via Docs RAG is quoting a spec revision that is **older than, and different from, the supplied v1.2** — ~17 months apart (1/11/23 → 6/20/24). I do **not** say 1.1 is *superseded*: that would assert 1.2 carries releasing authority, which is exactly what D7 leaves unresolved. Either revision could be the released one. What is certain is that the **1.1→1.2 delta is entirely invisible to RAG.** I will treat *every* RAG-sourced architectural requirement as **unqualified until it is checked against the governing original, once D7 establishes which revision governs.** Comparison against the on-disk v1.2 is **useful delta evidence, not the universal authority gate** — D7 permits v1.0 to be the governing revision, in which case a RAG quote of 1.1 may be closer to governing text than 1.2 is. I have extracted 1.2 to text locally and will diff any cited requirement against it as evidence.

This alone **blocks** a candidate that other agents agree on — it renders it **unqualified pending the authority check, not invalidated.** A stale RAG source is a gap in the evidence chain, not a disproof of the claim. Flagging to all peers.

### 2.2 DID filename understates its content — version-precision hazard

On-disk file: `Telluride_DID_1.5_VCU.docx`. Its **internal** revision history runs past the filename:

| Version | Date | Editor | Comments |
|---|---|---|---|
| 0.5.0 | 06/20/2022 | Anurag Agrawal | Initial draft |
| 0.9.0 | 10/27/2022 | Anurag Agrawal | — |
| 1.0.0 | 11/15/2022 | Anurag Agrawal | — |
| 1.1.0 | 02/10/2022 *(as printed; likely 2023)* | Anurag Agrawal | BPD POR implementation |
| 1.2.0 | 08/20/2023 | Anurag Agrawal | Sysmon implementation |
| 1.5.0 | 09/10/2023 | Anurag Agrawal | Interrupt tree, DFX arch updates, NPI interrupt scan/mem clear, partial reconfiguration proposal, area estimation |
| **1.5.1** | **07/15/2024** | Abhay Galagali | **EDT-1069519 fix: updated reset section. EDT-1072959 fix: updated Abus Switch DPSTx2 section** |
| **1.5.1** | **31/10/2025** | Abhay Galagali | **Cleaned up TBDs** |

Two notes, both load-bearing:

- **"DID 1.5" is ambiguous.** 1.5.0 (09/2023) and 1.5.1 (07/2024, further edited 10/2025) differ. Any candidate citing "DID 1.5" must state which. I will not accept the bare string.
- **1.5.1's revision row cites two EDTs** — `EDT-1069519` and `EDT-1072959`. What that row establishes is limited; see **§2.2a**. It is **not** a basis for rejecting candidates as already-fixed.

#### 2.2a — EDT-1072959: my earlier reading was WRONG. Rejection ground SUSPENDED.

**My error.** From the DID revision row I recorded `EDT-1072959` as *"Abus Switch DPSTx2"* and built a standing rejection ground on it. Flagged by orchestrator `[HISTORY]` and sentinel `[SENTINEL-NOTICE]`; I then queried Jira directly rather than accepting the flag on authority.

**Verified from Jira (direct, `sage.search_xilinx_jira`):**

| Field | Value |
|---|---|
| `EDT-1072959` subject | **"Reify Assertions Failures for the Power Aware Scenarios"** — *not* Abus Switch / DPSTx2 |
| Status | Closed |
| Labels | `FUSA_NO_IMPACT`, `REIFY_T50`, `rtl_risk` |
| Waiver ticket | `EDT-1076244` |
| Closure comment (Abhay Galagali) | integrated "the uniquified OR gate released by the PoR IP team. RTL, UPF, PD Netlist (DFT and LAY) complete." |

**Unresolved mismatch.** The DID revision row and the Jira subject describe different things. One candidate explanation is that the DID row labels *the DID section that was edited* while Jira labels *the underlying problem* — these would not strictly contradict. **I am not asserting that.** It is an untested hypothesis; the mismatch stands open.

**Standing rule (per sentinel, accepted):** *do not reject a candidate as already-fixed on the strength of a DID revision-history label alone.* Already-fixed (checklist 15) now requires the Jira record itself, or the fix visible in the supplied RTL.
- The date `02/10/2022` for v1.1.0 is out of sequence (post-dates 1.0.0 of 11/15/2022 only if read DD/MM). Minor, but I will not rely on that row's date.

Also captured: the DID's own **Open Issues** table records that arch spec ambiguity on *multi-core decoder* and *I/IP/IPB decoding* was resolved by email with Ygal — decoder is **single-core**, and decoder **must** support I, P and IPB while the encoder may pick features. That is a documented spec-ambiguity resolution living outside the spec; candidates in that area should be checked against it rather than against the spec text alone.

### 2.4 DID pins architecture spec **v1.0** while supplied spec is **v1.2** — AUTHORITY CHAIN UNRESOLVED

Orchestrator reported this. **I verified it independently against the DID rather than accepting it**, and it is confirmed — with a nuance that changes its interpretation.

The DID's *Reference Documents* table, row "Architecture Specifications", contains **two** hyperlinks:

1. A **version-agnostic folder** link — `amdcloud.sharepoint.com/.../Telluride/VCU/Arch specs`
2. A **direct, version-pinned file** link — `xilinx.sharepoint.com/.../Arch Specs (released)/Telluride/MMD/`**`Everest_MMD_VCU_Arch_Specs_1.0.pdf`**

So the DID — internally at **1.5.1, last edited 31/10/2025** — still carries a **version-pinned reference** to **arch spec 1.0**, while the spec supplied to this audit is **1.2 (6/20/24)**. What this establishes is a pinned reference and an **unresolved authority chain** — *not* that 1.0 is the DID's authoritative architectural input, and not that 1.2 supersedes it.

The nuance that matters: the 1.0 link resolves into a folder explicitly named **"Arch Specs (released)"**. That admits two readings, and they have opposite consequences:

- **Reading A — 1.2 was never formally released.** The DID's 1.0 pin is then *correct*, and arch spec 1.2 is a draft/working revision with no sign-off authority. Under this reading, candidates citing 1.2 requirements fail checklist item 1 (requirement not authoritative).
- **Reading B — 1.2 was released and the DID reference was never refreshed.** The DID pin is then a stale citation → `DOCUMENTATION_BUG`, and 1.2 governs.

**I will not choose between these by inference.** The distinction determines which document is authoritative for *every* architecture-sourced candidate in this audit, and guessing it would corrupt every downstream verdict. It is escalated as blocker **D7**.

Two secondary observations: the pinned link is on the **legacy `xilinx.sharepoint.com` tenant** while the folder link is on **`amdcloud.sharepoint.com`**, consistent with a partially-migrated document set; and note that **RAG independently holds only 1.0 and 1.1** (§2.1), which is at least *consistent* with Reading A — but corpus contents are not release evidence, and I do not treat that coincidence as support.

### 2.3 VCU DID appears absent from RAG

Querying RAG for the Telluride VCU DID returns `Everest_DID_MMI_v{0.5,1.0,1.5}.docx` — the **MMI (Multi-Media Integrated) module** DID, a *different document* from `Telluride_DID_1.5_VCU.docx`. I have no positive evidence the VCU DID is indexed at all. Treat RAG as non-authoritative for DID content; I am reading the on-disk docx directly.

---

## 3. RTL identity — UNRESOLVED, HARD BLOCKER

Root: `/everest/psival_verif_nobkup/manish/VCU_VERIF_12_MARCH/VCU_VERIF_12MARCH/shadow/`

- Version control is **Perforce**, not git. `.icmconfig` → `P4CLIENT=yadav+everest+v1+35`, `P4PORT=xhdicmsuper:1777`. IC Manage over P4.
- `git rev-parse` → *not a git repository*.
- **The `p4` binary is not present on PATH in this session.** No `p4 changes`, `p4 have`, `p4 sync -n`, `p4 fstat`. **I cannot pin an immutable changelist.**

What I have instead is mutable filesystem metadata, which is *not* a version identity:

| Path | mtime | Owner |
|---|---|---|
| `shadow/` | Aug 4 16:28 | yadav |
| `shadow/rtl/` | Jul 21 11:08 | yadav |
| `shadow/rtl/vcu2_v1_7t/` | Jul 21 11:08 | yadav |
| `vcu2_v1_7t/*` (contents) | **Mar 12 12:00** | yadav |
| `shadow/verif/` | Aug 11 11:32 | yadav |

Workspace name and directory both say **12 MARCH**, consistent with the Mar 12 content mtimes.

**Blocker statement (checklist item 4 — "Is the RTL branch and commit correct?"):** this cannot be answered. A shadow workspace is a *mutable working view*; without `p4 have` I cannot prove which changelist any file is at, nor that the tree is internally consistent, nor that it corresponds to any tapeout release. Directory mtimes spanning Mar 12 → Aug 11 **could result from multiple causes** — partial re-sync, in-place edits, metadata-only touches, or ordinary tooling writes. They **cannot prove sync chronology, and cannot establish that the tree is or is not a mixture of changelists.** They are recorded as observation only and carry no evidentiary weight in either direction.

**Consequence for verdicts:** on **current** evidence I cannot support `CONFIRMED_SPEC_RTL_MISMATCH` or `HIGH_CONFIDENCE_SILICON_BUG`, and the strongest available verdict is `NEEDS_SIMULATION_OR_FORMAL_PROOF`. **This is an evidentiary limit, not a mechanical prohibition** — correcting an over-absolute earlier statement. The user's enum contract does not condition any verdict on Perforce. If a **byte-frozen analyzed closure** (explicit file set with hashes) were established and independent proof met the bar against *that* closure, a mismatch scoped to that closure could in principle be supported, with target applicability still blocked separately under D2. Absent that, I will not launder an unpinned tree into a sign-off. Resolution requires one of: `p4` on PATH, a `p4 have`/`p4 changes -m1` dump from someone with the client, or an authoritative release tag.

### 3.1 VCU2 RTL structure (mapped, for future candidate routing)

`rtl/vcu2_v1_7t/` — 43 subdirectories. Design hierarchy:

`vcu2_core`, `vcu2_core_top`, `vcu2_enc_top`, `vcu2_enc_core_top`, `vcu2_dec_top`, `vcu2_apm`, `vcu2_atom`, `vcu2_bpd_por`, `vcu2_int_wrap`

Supporting collateral, all directly relevant to specific checklist items:

- `upf/` — power intent. Required for any power/retention/isolation candidate.
- `regspecs/` — register definitions. **Potential fallback for XRDB (down, §4); provenance and XRDB parity unaudited — D9.** Not a validated substitute.
- `constraints/` — SDC; required for CDC/RDC reasoning (item 12).
- `include/` — macros/params; required for item 6 (are generate conditions active).
- `interface/`, `int_map/` — interrupt mapping; pairs with DID 1.5.0 interrupt tree.
- `mbist/`, `memlib/`, `scan_setup/`, `030_dc_dft/`, `spyglass_dft/` — DFT/memory.
- `tb/`, plus `verif/vcu2_v1_7t/{common, vapm, vcu, vcu2_xlxwall_qual_test, xlxbuild_setup, xlxwall_setup, run_xlxqual, run_xlxsubmit}`.

8,212 `.v`/`.sv` files across the shadow tree (all IPs, not VCU-only).

**Not yet done:** I have not elaborated any RTL path, resolved any macro, or confirmed any generate condition is active. Checklist items 5–7 are open for all future candidates and will be worked per-candidate.

---

## 4. MCP / source accessibility — measured, not assumed

Each probed with a live call this session.

| Source | Status | Evidence | Impact on verdicts |
|---|---|---|---|
| **RTL filesystem** | ✅ UP | Tree walked, structure mapped | Usable for code reading; **not** for version identity (§3) |
| **Arch spec 1.2 (disk)** | ✅ UP | 1,321 lines extracted via `pdftotext` | Supplied candidate primary source; authority/applicability unresolved (D2/D7) |
| **DID 1.5.1 (disk)** | ✅ UP | 169,337 chars extracted via docx/XML | Supplied candidate primary source; authority/applicability unresolved (D2/D7) |
| **`sage.search_docs`** | ⚠️ **UP BUT STALE** | Returns arch 1.0/1.1 only; VCU DID absent | **Leads only.** Every hit must be re-verified on disk (§2.1) |
| **`sage.search_xilinx_jira`** | ✅ UP | 9,445 issues; live PVS/EDT/CR/EVVNC | Primary defect-history channel. **Serves items 14/15** |
| **Confluence (`cloud_atlassian`)** | ✅ UP | Returned XPS pages: "VCU2/ISP TST", "T40 VCU Day 1 Bringup", "VCU Baremetal Firmware" | Usable, secondary |
| **GitHub MCP** | ✅ UP | Authenticated as `karthick_amdeng` | Usable; relevance to P4-hosted RTL unproven |
| **Jira (`cloud_atlassian`)** | ❌ **DOWN** | `401 Unauthorized — Invalid user Jira token` | **Mitigated:** `search_xilinx_jira` covers the same projects |
| **XRDB (all projects)** | ❌ **DOWN** | `Unable to connect. Is the computer able to access the url?` | **Register-level ground truth unavailable — UNMITIGATED.** On-disk `rtl/vcu2_v1_7t/regspecs/` is a **potential fallback, not yet validated**: its provenance and parity with XRDB are unaudited, and Atlas has not audited it either. Availability of a substitute is not mitigation. → **D9** |
| **Perforce (`p4`)** | ❌ **ABSENT** | `env: 'p4': No such file or directory` | **Hard blocker** — no immutable RTL identity (§3) |

**One failure is mitigated:** Jira → `search_xilinx_jira` (verified working; I used it to correct EDT-1072959). **Two are not.** (a) **XRDB — UNMITIGATED.** Register-source coverage remains blocked/evidence-pending; `regspecs/` is an unvalidated potential fallback whose provenance and XRDB parity must be audited first (**D9**). (b) **Absent `p4`** — limits confidence and applicability (**D1**); see §3 for the corrected, non-mechanical statement of what that does and does not foreclose.

---

## 4A. Provenance register — every Phase 0 finding, with direct-vs-relayed attribution

All items below are **evidence cautions**, not candidates and not verdicts. None is a defect claim.

"**Direct**" = I read the primary artifact myself in this session. "**Relayed → verified**" = a peer asserted it and I then independently re-derived it from the primary artifact. Nothing in this audit is recorded on relay alone.

| # | Finding | Primary source | Locator | Verbatim excerpt / extracted value | Provenance |
|---|---|---|---|---|---|
| P1 | Architecture-axis identity: Everest MMD VCU = Telluride VCU = VCU2, distinct from Diablo ZU+ VCU | `Everest_MMD_VCU_Arch_Specs_1.2 (1).pdf` (23 pp) | **p. 4, §1.1 Introduction** | "The MMD VCU is part of the Telluride family of products. The MMD VCU is sometimes referred to as VCU2, to distinguish it from the original Diablo (Zynq Ultrascale+) VCU." | **Direct** |
| P2 | Physical placement / process, supporting P1 | same | **p. 4, §1.1** | "…designed to fit within the footprint of two GTY quad modules and can replace GTY quad pairs along the right edge of an Everest (Telluride) SoC device." / "Target process technology is 7nm." | **Direct** |
| P3 | Diablo→VCU2 delta list (7 areas where Diablo-derived claims require independent VCU2 re-derivation) | same | **p. 4, §1.1** | "Improved encoding quality / Re-architected cores… / 2 encoder cores per encoder instance / Addition of frame buffer compression / Hardened encoder 'L2 cache' / Interlaced support / 4:4:4 color format support" | **Direct** |
| P4 | Third-party IP ownership boundary | same | **p. 7, §1.5.1 IP Configuration** | "The following are the chosen Encoder and Decoder IP configurations provided by Allegro DVT." | **Direct** |
| P5 | Arch spec revision chain; 1.2 dated 6/20/24 | same | **p. 3, Revision History table** | 0.4 (3/1/21) → 0.5 (4/30/21) → 0.7 (6/30/21) → 1.0 (10/1/21) → 1.1 (1/11/23) → **1.2 (6/20/24)**, author Ygal throughout | **Direct** |
| P6 | DID internal version is 1.5.1, not the filename's 1.5 | `Telluride_DID_1.5_VCU.docx` (169,337 chars extracted) | *VCU DID Revision History* table | "1.5.1 | 07/15/2024 | Abhay Galagali" and "1.5.1 | 31/10/2025 | Abhay Galagali | Cleaned-up TBDs" | **Relayed (orchestrator) → verified** |
| P7 | ~~EDT duplicate trap — reset & Abus Switch DPSTx2 already absorbed~~ **RETRACTED (§2.2a).** Row quoted correctly; the *inference* drawn from it was wrong. `EDT-1072959` is "Reify Assertions Failures for the Power Aware Scenarios" per Jira. | same | *VCU DID Revision History*, row 1.5.1 (07/15/2024) + Jira `EDT-1072959` | "EDT-1069519 fix: Updated reset section. EDT-1072959 fix: Updated Abus Switch DPSTx2 section" | **Direct** (both) |
| P8 | DID pins arch **1.0** while supplied spec is 1.2 | same | *Starting References → Table 2: Reference Documents*, row "Architecture Specifications" | Two links. Folder: `amdcloud.sharepoint.com/…/Telluride/VCU/Arch specs`. Pinned file: `xilinx.sharepoint.com/…/`**`Arch Specs (released)`**`/Telluride/MMD/`**`Everest_MMD_VCU_Arch_Specs_1.0.pdf`** | **Relayed (orchestrator) → verified, and extended** — the "(released)" folder name is mine, and it is what makes the item ambiguous rather than simply stale (§2.4) |
| P9 | Decoder single-core; I/P/IPB decode — resolved by email *outside* the spec | same | *Open Issues* table, items 1–2 | "Multi core decoder — In the architecture specs, it is defined that decoder has only one core… Take this data from Ygal. Email sent." / "Decoder must decode any stream, while encoder can pick and choose features. Decoder must support I, P and IPB decoding." | **Direct** |
| P10 | Arch spec 1.2 has its own unresolved Open Issues | `…Arch_Specs_1.2` | **p. 7, §1.4 Open Issues** | "Power domain — Open TBD"; "DPLL jitter — Open, need to check if design was improved"; frame-buffer-compression module / TBU / interlaced display — Closed | **Direct** |
| P11 | Docs RAG stale: indexes arch 1.0 & 1.1 only, 1.2 absent; VCU DID absent | `sage.search_docs` live queries ×2 | — | Returned `…Arch_Specs_1.0.pdf`, `…_1.1.pdf`; DID query returned `Everest_DID_MMI_v{0.5,1.0,1.5}.docx` — the MMI module DID, a different document | **Direct** (tool observation) |
| P12 | No immutable RTL identity | `shadow/.icmconfig`; shell probes | — | `P4CLIENT=yadav+everest+v1+35`, `P4PORT=xhdicmsuper:1777`; `git rev-parse` → "not a git repository"; `p4` → "No such file or directory" | **Direct** |
| P13 | Possible mixed-changelist tree | filesystem mtimes | — | `shadow/` Aug 4 · `rtl/` Jul 21 · `vcu2_v1_7t/` contents **Mar 12** · `verif/` Aug 11 | **Direct** — inference from mtimes is *suggestive only*; mtimes are not version identity |

P10 is newly surfaced here: **arch spec 1.2 itself carries open TBDs** on *power domain* and *DPLL jitter*. Candidates landing in those two areas may hit `SPEC_AMBIGUITY` rather than a defect, because the specification does not yet state a required behavior to violate. Registering it now so it is not mistaken for a finding later.

---

## 4B. INV-RESET-ISOLATION-01 — pre-candidate evidence chain (NOT a candidate, NO verdict)

Opened by orchestrator as an **evidence chain**. No candidate has been formally submitted, so **no verdict is issued here.** Recorded so that whoever does submit one inherits verified facts rather than repeating the trace.

**Requirement under examination.** Arch spec 1.2, p.10 §1.6, normative *shall*:

> "Encoder and decoder operation shall be completely independent, e.g., each may operate or be idle or disabled or reset independently without affecting the other, and each shall have independent clocks."

**E1 — Independent HW reset inputs exist in the supplied source RTL.** Directly read, not relayed:

`vcu2_core_top/rtl/vcu2_core_top.sv:1010-1015`
```systemverilog
//abhayg-06-17-24: Add seperate resets for ENC and DEC
wire pl_vcu2_enc_raw_rst_n;
wire pl_vcu2_dec_raw_rst_n;

// PD recommendantion is to use [56] for ENC and [11] for DEC reset inputs.
// These sites are very close to the reset AND gates and will give better timing.
assign pl_vcu2_enc_raw_rst_n = pl_vcu2_spare_in[56];
assign pl_vcu2_dec_raw_rst_n = pl_vcu2_spare_in[11];
```

This **corroborates the DID 1.5.1 reset table** (`spare_in[56]` = ENC RAW RST, `spare_in[11]` = DEC RAW RST) against RTL. The DID is accurate on this point.

**E2 — The reset cones are structurally disjoint.** `vcu2_reset/rtl/vcu2_reset.sv:64-89`, pure combinational AND, no shared sequential state:

```systemverilog
vcu2_enc_rstn_i = reset_raw_iporpcsr_initstate_porb & ssc_fuse_valid_enc_enable;   // and2_reset_6a
vcu2_enc_rstn   = vcu2_enc_rstn_i & pl_vcu2_enc_raw_rst_n;                          // and2_reset_6
vcu2_dec_rstn_i = reset_raw_iporpcsr_initstate_porb & ssc_fuse_valid_dec_enable;   // and2_reset_7a
vcu2_dec_rstn   = vcu2_dec_rstn_i & pl_vcu2_dec_raw_rst_n;                          // and2_reset_7
```

There is **no path** from `pl_vcu2_enc_raw_rst_n` to `vcu2_dec_rstn`, nor to `vcu2_interconnect_rstn` (which is gated by the OR of the two enables, `and2_reset_5`, and by neither raw reset). Symmetrically for DEC. **At the reset-generation level, independence holds in the supplied source.**

**E3 — Chronology is coherent.** RTL comment `abhayg-06-17-24` → `EDT-1069519` closed 07/15/2024 → DID 1.5.1 dated 07/15/2024 documenting the pins. The Jira *description* ("present implementation is single hardware reset… need to choose between SW solution or independent HW reset") states the **original problem**, not the outcome. The RTL shows the HW option was taken. This dissolves the apparent SW-only-closure contradiction.

**E4 — The `_with_fix` wrapper variant is NOT the reset fix, and is NOT compiled.** All three filelists (`vcu2_int_wrap.filelist`, `vcu2_core_vcu2_int_wrap.filelist`, `vcu2_int_wrap_mbist.filelist`) reference `vcu2_int_wrap.sv`; **none** reference `vcu2_int_wrap_with_fix.sv`. The two files differ by 21 diff lines concerning **`EDT-1094051`, an EOF-pulse fix** — unrelated to reset. Checklist item 6 is therefore satisfied *for the reset question*: the reset logic is in `vcu2_core_top.sv`/`vcu2_reset.sv`, not behind a variant selection. **Separately noted, not investigated:** the compiled base file has the `EDT-1094051` block commented out and uses a different `.sync_eof` source (`..._sync_with_clk` vs `..._sync_re`). Whether that is a later supersession or a dropped fix is **unknown and out of scope here.**

**E5 — Implemented-netlist confirmation is BLOCKED, not satisfied.** Per the standing netlist-is-source-of-truth rule I attempted to confirm E1/E2 in an implemented netlist. `030_dc_dft/vcu2_core/vcu2_core.v` is dated **`Mon Jun 5 20:14:27 2023`** in its Synopsys DC header — **one year before** the `06-17-24` reset change — and contains **no `enc_raw_rst_n`/`dec_raw_rst_n`**. It also ties its inner `pl_vcu2_spare_in` to all-zeros, but as a **pre-fix DFT netlist this proves nothing about the shipped reset path in either direction.** No post-fix netlist is available to me → **D8**. Until one is read, "the fix is present in silicon" is **unproven**; E1/E2 establish only *source* intent.

**What this chain does and does not support.** It supports rejecting a *missing-connectivity* hypothesis (i.e. "spare_in[56]/[11] are not wired to independent reset cones") — that hypothesis is contradicted by E1/E2. It does **not** establish compliance with §1.6, because §1.6 requires the engines not to affect each other **operationally**, and reset-cone disjointness is necessary but not sufficient. Untested: shared AXI interconnect state and in-flight bursts when one engine is reset mid-transaction, shared MCU, IRQ behaviour, clock independence (a separate §1.6 clause), and re-initialisation. Those are open.

---

## 4C. EDT-1094051 EOF-pulse divergence — NEW pre-candidate observation (NOT a candidate, NO verdict)

Discovered incidentally while resolving which wrapper variant compiles for INV-RESET-ISOLATION-01. Kept **separate** from the reset review per orchestrator direction — it is unrelated to reset and must not be buried there. Handed to Orion/Trace/Echo for triage; I claim no defect.

**Source identifiers.**

| Item | Value |
|---|---|
| Compiled file | `rtl/vcu2_v1_7t/vcu2_int_wrap/rtl/vcu2_int_wrap.sv` (mtime Jun 18 11:49) |
| Non-compiled sibling | `rtl/vcu2_v1_7t/vcu2_int_wrap/rtl/vcu2_int_wrap_with_fix.sv` (mtime Mar 12 12:00) |
| Third variant | `vcu2_core_vcu2_int_wrap_wo_fix.sv`, under the mbist `tsdb_outdir` DFT tree |
| Filelists | `vcu2_int_wrap.filelist:70`, `vcu2_core_vcu2_int_wrap.filelist:70`, `vcu2_int_wrap_mbist.filelist:70` — **all three reference `vcu2_int_wrap.sv`; none reference `_with_fix.sv`** |
| History tag | `//abhayg: EDT-1094051. Fix for EOF pulse requirement` |
| Divergence | 21 diff lines, at `:737-740`, `:2228-2229`, `:4981-4992` |

**The observation.** In the **compiled** file the `EDT-1094051` fix block at `:737-740` is **commented out** (`/* … */`), as is the `always @(posedge vcu2_dfx_enc_clk, negedge vcu2_enc_interface_rstn_out)` block at `:4981-4992`. The `.sync_eof` port connection also differs:

```systemverilog
// compiled  vcu2_int_wrap.sv:2228-2229
//.sync_eof ({vcu2_enc_sync_eof_sync_re[0], vcu2_enc_sync_eof_sync_re[1]}),
  .sync_eof ({vcu2_enc_sync_eof_sync_with_clk[0], vcu2_enc_sync_eof_sync_with_clk[1]}),

// non-compiled  vcu2_int_wrap_with_fix.sv:2228
  .sync_eof ({vcu2_enc_sync_eof_sync_re[0], vcu2_enc_sync_eof_sync_re[1]}),
```

**What is established:** the file named `_with_fix` is **not** what builds, and the file that builds has the `EDT-1094051`-tagged block disabled while sourcing `.sync_eof` from `..._sync_with_clk` instead of `..._sync_re`.

**What is NOT established, and must not be inferred:** whether this is a **later supersession** (the `_with_clk` path being a better re-implementation, with the old flop-resync correctly retired) or a **dropped/reverted fix** (regression). The compiled file is *newer* by mtime, which mildly favours supersession — but per the corrected §3 rule, mtimes carry no evidentiary weight. The confusing filename `_with_fix` invites the wrong reading in both directions.

### 4C.1 Additional evidence gathered 2026-08-12 (pre-candidate; no verdict, hold respected)

Gathered in response to a targeted evidence question from `defect-echo`. **This is evidence-register work under the orchestrator's explicit allowance for §4C, not candidate reconstruction.**

**E6 — Jira record, read directly (not via RAG).** `EDT-1094051` "Sync EOF should last exactly one clock cycle", **Closed**, assignee Erusalagandi Srikanth, created **2025-12-15**, updated **2026-07-31**, labels `IMT_baseline_difference`, `fixed_in_imt50`, `security_review_closed`. Description: Allegro clarified that **both** `sync_eol` and `sync_eof` must last **exactly 1 encoder clock** (950 MHz); PL DMA sources at ~300 MHz cannot meet this. Verification comments (Ammula, Kalluri) describe assertion-checked **single-cycle pulse generation** at 330/300/150/75/37.5 MHz source rates; Erusalagandi reports presilicon pass "with T50 Fix", `src_sync` (`0xE8040060`) = `0x13FF`.

**E7 — EOL/EOF asymmetry inside the compiled file.** In `vcu2_int_wrap.sv` the **EOL** rising-edge pulse generator is **active** (`:4949-4960`, tagged `EDT-1059312`), while the structurally identical **EOF** generator immediately below is **commented out** (`:4980-4992`). Both signals carry the same Allegro exactly-one-cycle requirement per E6. In the compiled file `.sync_eof` (`:2229`) is driven directly from the `xil_sc_sync3` outputs (`:4962-4978`) — a **synchronized level**, with no narrowing in this module.

**E8 — the narrowing exists, relocated to PL-side soft IP, and is CONFIGURATION-GATED.** `vcu2_vivado_LLM_wrap/rtl/vcu2_v3_0_rfs.v:1372-1396`:

```verilog
generate
if (!ONE_INSTANCE) begin : GEN_ONE_INSTANCE
    if (LLP_MODULE) begin : GEN_LLP
      vcu2_v3_0_2_lc_sync_module sync_module( .sync_clk(sync_clk), .src_pulse_path0(c0_enc_sync_eof_path0), ... );
    end
  else begin : GEN_NO_LLP
      assign eof_pulse_path0 = c0_enc_sync_eof_path0;   // raw pass-through
  end
end
else begin : GEN_NOT_ONE_INSTANCE
    assign eof_pulse_path0 = c0_enc_sync_eof_path0;     // raw pass-through
end
endgenerate
```

`vcu2_v3_0_2_lc_sync_module` (same file) uses `xpm_cdc_single` into `capture_clk` followed by **`xpm_cdc_pulse` into `sync_clk`** — which does produce a one-destination-clock pulse in the fast domain. So the EOF narrowing was **relocated out of the hard-IP wrapper into PL soft IP**, which reframes the question: the primary axis is **not** supersession-vs-drop, but **under which generate configuration the narrowing exists at all.** In `ONE_INSTANCE`, and in `!LLP_MODULE`, both branches are raw pass-through and **no narrowing exists on either side** — checklist item 6 (generate conditions active) is the governing question, and it is **open**.

**E9 — what the closure evidence corresponds to.** The E6 verification comments describe assertion-checked single-cycle behaviour across 330 → 37.5 MHz. A synchronized **level** cannot produce that; the PL `xpm_cdc_pulse` path or the commented-out core edge-detect can. This is an **inference from signal structure, not an executed check** — I have compiled, simulated and proven nothing. It indicates the closure evidence corresponds to a **pulse-generating configuration**, and does *not* establish that it corresponds to the specific PL relocation above rather than the retired core-side block.

**E10 — SELF-FALSIFYING evidence against the pass-through concern (PROVER-DIRECT, 2026-08-12).** Checklist item 17 discipline: I looked for what would disprove my own E8 framing, and found it. Resolved parameters and port drivers for all three on-disk Vivado wrappers:

| Wrapper | `ONE_INSTANCE` / `LLP_MODULE` | Branch taken | `c0_enc_sync_eof_path0/1` driver |
|---|---|---|---|
| `vcu2_vivado_LLM_wrap` | `0` / `1` (`design_1_vcu2_0_0.v:890-891`) | `GEN_LLP` — **narrowing present** | Real top-level inputs (`:638-639`, `:1106`) |
| `vcu2_vivado_enc_wrap` | `0` / `0` (`:586-587`) | `GEN_NO_LLP` — pass-through | **`1'B0` tie-off** (`:802-803`) |
| `vcu2_vivado_dec_wrap` | `0` / `0` (`:586-587`) | `GEN_NO_LLP` — pass-through | **`1'B0` tie-off** (`:802-803`) |

Module defaults are `ONE_INSTANCE = 0`, `LLP_MODULE = 1` (`vcu2_v3_0_rfs.v:86-87`) in all three copies; the `0` values are explicit instantiation overrides.

**This materially weakens the concern and I record it as such.** In every on-disk configuration the branch selection is *consistent with usage*: the one wrapper with a live EOF source takes the narrowing branch, and both wrappers taking the pass-through branch have **no EOF source at all** (tied to zero, EOL likewise). That is coherent design, not a gap. My earlier "two of three branches have no narrowing" statement, while literally true of the generate structure, **overstates the practical exposure** and must not be propagated without this qualifier.

**What remains genuinely open** is narrower than before: whether any *supported* configuration exists that combines a **live EOF source** with `LLP_MODULE = 0` or `ONE_INSTANCE = 1`. **On disk, none was found.** Absence in this tree is not proof of absence in the target build (D1, D2) — but the burden now sits on anyone asserting such a configuration exists, not on the design.

---

### E10-R — E10 CORRECTED AND SUPERSEDED (PROVER-DIRECT re-read, 2026-08-12)

Trace, via the orchestrator, challenged E10. I re-read every cited declaration and port driver myself. **The outcome is mixed and I record both halves.**

**(a) Trace's stated grounds are incorrect — file conflation.** Trace reports "all four `vcu2_v3_0_rfs` files default `ONE_INSTANCE=0`/`LLP_MODULE=1`". That is true and is exactly what E10 line 360 already said: `vcu2_v3_0_rfs.v:86-87` is the **module default**. It is overridden at instantiation in `design_1_vcu2_0_0.v`. Re-verified verbatim this session:

| Wrapper | `design_1_vcu2_0_0.v` override | line |
|---|---|---|
| `vcu2_vivado_LLM_wrap` | `.ONE_INSTANCE(0), .LLP_MODULE(1)` | `:890-891` |
| `vcu2_vivado_enc_wrap` | `.ONE_INSTANCE(0), .LLP_MODULE(0)` | `:586-587` |
| `vcu2_vivado_dec_wrap` | `.ONE_INSTANCE(0), .LLP_MODULE(0)` | `:586-587` |
| `vcu2_vivado_wrap` | `.ONE_INSTANCE(0), .LLP_MODULE(0)` | `:882-883` |

Overrides govern. The `1'B0` tie-offs are also real, verbatim: `enc_wrap`/`dec_wrap` `:802-803`, `vcu2_vivado_wrap` `:1098-1099`. **E10's stated facts are not "factually wrong".**

**(b) Trace's XMR observation is correct, and it invalidates E10's reasoning — I traced the wrong net.** In `enc_wrap`, `dec_wrap` and `vcu2_vivado_wrap`, the core instance's EOF input is **not** fed from `c0_enc_sync_eof_path0/1` at all. `vcu2_v3_0_rfs.v:956` reads:
```verilog
.vcu2_enc_sync_eof ( vcu_top_wrapper.i_glbl_intf.vcu2_enc_sync_eof ),
```
— an unguarded hierarchical reference (no `ifdef`, no `translate_off`; the surrounding block drives jtag, mcu and debug_clk the same way). Only `LLM_wrap:956` uses the narrowed net: `.vcu2_enc_sync_eof({eof_pulse_path1, eof_pulse_path0})`.

So the `1'B0` tie-off sits on a **wrapper port whose value never reaches the core** in those three. My inference "pass-through branch ⇒ no EOF source at all" was drawn from a dangling net. **A live EOF source with `LLP_MODULE = 0` and no narrowing does exist on disk.**

**(c) Corrected disposition.** E10's *grounds* are withdrawn; its *direction* survives on different grounds. The three XMR-driven wrappers are **simulation harnesses**, not product configurations — an unsynthesizable cross-module reference into `vcu_top_wrapper.i_glbl_intf` cannot be a customer build. So: **live-EOF + non-narrowing is demonstrated in the sim environment, and no product live-pass-through is demonstrated.** I withdraw the E10 sentence "the burden now sits on anyone asserting such a configuration exists" — that burden-shift was earned by the tie-off reading and does not survive it.

**(d) Scope error in E10 acknowledged.** E10 said "all three on-disk Vivado wrappers". There are **four** (`vcu2_vivado_wrap`, added above), plus `vcu2_vivado_LLM_wrap_bkp_29apr` and several `rtl_*_bkp` / `rtl_25may` / `rtl_11may_bkp` trees not audited. "All on-disk" was not established and should not have been claimed.

**Credit and caution, stated plainly:** Trace found the real defect in E10 while giving a reason that does not hold. Neither half should be dropped — accepting the challenge wholesale would have written a false parameter claim into canonical; rejecting it wholesale would have preserved a conclusion built on a net I mis-traced. **This correction was found by re-reading, not by adjudicating between two agents' assertions.**

**E11 — the remaining open question is NOT answerable from this tree (PROVER-DIRECT bounded negative result, 2026-08-12).** The one live question after E10 is whether `LLP_MODULE` / `ONE_INSTANCE` are **user-selectable in the packaged Vivado IP** — if a customer can configure a live EOF source with `LLP_MODULE = 0`, the exposure is real; if the parameter is fixed at packaging, it is not. I searched for the IP packaging that would answer this:
- `find` for `component.xml` / `*.xit` / `xgui/*.tcl` under `rtl/vcu2_v1_7t/` → **no hits**.
- `grep -rl "LLP_MODULE"` across all `*.xml` / `*.tcl` in `rtl/vcu2_v1_7t/` → **no hits**.
- Bounded `find` at shadow root (`maxdepth 6`) → `component.xml` exists **only** under `rtl/hnic_v1_7t/`; **none for VCU2**.

**Conclusion: VCU2 Vivado IP packaging is absent from this workspace.** The supported-configuration question therefore cannot be closed in either direction from the supplied tree — this is an **evidence dependency, not a negative finding about the design**. Resolving it requires the packaged IP (`component.xml` / `xgui` Tcl) or the IP product guide. → folded into **D2** (target configuration identity).

**E12/E13 — Echo's packaging-authority claim, independently re-read from source (PROVER-DIRECT, 2026-08-12).** Echo reported (E12) that Confluence TDL page **753167822 v23, "VCU2 IP Wizard Document"** answers E11 in the reassuring direction. Per standing rule "verify RAG/peer results against primary sources", I did **not** accept the quote. **Tool-limit correction: the `cloud_atlassian` 401 applies to Jira only — `confluence_get_page` on this page succeeded.** I read page 753167822 v23 in full, myself.

**Confirmed verbatim from the page (Echo's quotes are accurate):**
- User Parameters List: `C0_ENC_ENABLE_LOW_LATENCY_MODE` | Default **False** | Range True/False | "Encoder Low Latency Mode Configuration".
- VCU Ports table: `c0_vcu2_enc_sync_eol[1:0]` and `c0_vcu2_enc_sync_eof[1:0]`, Input, "*Enabled when C0_ENC_ENABLE_LOW_LATENCY_MODE == True && C0_ENABLE_ENCODER == True*".
- `ONE_INSTANCE` does **not** appear anywhere on the page — not in the User Parameters List, not in the port table. Echo's own caveat is correct.

**One detail Echo did not report, which strengthens their case:** under Encoder Options → Other Configuration the page reads "**Low Latency Mode(Encoder)<TRUE/FALSE>: User can enable sync IP with this switch**" / "FALSE: **Disables sync IP support**". That ties the switch to a *sync IP block*, not merely to port visibility. Identifying that "sync IP" with `vcu2_v3_0_2_lc_sync_module` is **my inference, not the document's statement** — the page never names the module and never names `LLP_MODULE`.

**Three things E12 does NOT establish, and the drift I am flagging:**
1. **The document never states the `C0_ENC_ENABLE_LOW_LATENCY_MODE → LLP_MODULE` mapping.** The orchestrator's relay hardened this to "low-latency corresponds to LLP narrowing branch"; that correspondence is an **inference about the packaging script**, not a documented contract. The artefact that would state it is the `component.xml` / `xgui` Tcl — the very thing E11 established is absent. E12 supplies **port gating**; the narrowing-branch gating is still inferred. My E10 on-disk table is two generated instances consistent with the mapping — corroboration of the generator's output, not the generator's rule.
2. **`ONE_INSTANCE = 1` remains wholly unaddressed.** It is not a user parameter on this page, and the page's section heading "*Core top level Signaling Interface for **single instance***" implies a multi-instance case documented elsewhere. That RTL branch also bypasses narrowing. Open.
3. **E12 does not touch E7 at all.** Port gating governs the **PL wrapper**. E7 is a **core-internal** finding in `vcu2_int_wrap.sv`: EOL keeps its rising-edge detect (`:4959`), EOF's is commented out (`:4981-4992`), and the port connection at `:2229` carries the synchronised **level**. In the *only* configuration where EOF is live — low-latency **True** — that core-internal path is exercised. Whether a one-cycle pulse emerging from the PL-side sync module survives the subsequent `xil_sc_sync3` re-synchronisation into `vcu2_dfx_enc_clk` as a single enc-clock cycle depends on the `sync_clk` : `vcu2_dfx_enc_clk` frequency relationship, **which I have not established**. This is the live technical question and E12 does not bear on it.

**Net effect on E11: narrowed, not closed.** E12 is real evidence and I accept the port-gating fact as PROVER-DIRECT. It does not discharge D2, because the page carries **empty created/updated metadata** (attachments date to 2024, page is v23) and sits in the *Technical Documentation & Localization* space — a documentation-authoring source, not a released product guide pinned to a target SKU or stepping. **D1/D2 unchanged.** Symmetric-trap note: E12 points in the reassuring direction, which is exactly when the standard must not relax.

### E14 — `sync_clk` : `enc_clk` relationship RESOLVED for the LLM build (PROVER-DIRECT, 2026-08-12)

This is the question I flagged as live and unestablished when rejecting E12 as a closure. I resolved it from the RTL, **from parameter values, not from source comments** — the comments at `vcu2_v3_0_rfs.v:857-858` ("vcu_enc_clk/3", "same as vcu_enc_clk") turn out to be accurate, but they were not treated as authority.

**Clock chain, `vcu2_vivado_LLM_wrap`:**
- `sync_clk` ← `BUFG_GT` ← `clk_out1` ← **DPLL2 `CLKOUT1`**, `CLKOUT1_DIVIDE = 3` (`design_1_vcu2_0_0.v:908`)
- `capture_clk` ← `BUFG_GT` ← `clk_out0` ← **DPLL2 `CLKOUT0`**, `CLKOUT0_DIVIDE = 9` (`:907`)
- `c0_vcu2_enc_clk` ← **DPLL2 `CLKOUT2`**, `CLKOUT2_DIVIDE = 3` (`:909`) — *same DPLL instance*, `C0_DPLL_VCU2V2_inst`, `rfs.v:848-850`
- `c0_vcu2_enc_clk` → core port `.vcu2_enc_clk` (`rfs.v:1147`) → `vcu2_int_wrap.sv:210` → `vcu2_clk_mux ssss_i_vcu2_enc_clk_mux` (`:2503-2506`, `clk_in1`) → `vcu2_enc_clk_out` → `vcu2_dfx_enc_clk`, the clock of the E7 logic. **That block is a mux + ICG — no divider.**

**Two findings:**
1. **`sync_clk` and the encoder clock are the same frequency and come from the same PLL** — identical `CLKOUT` divide of 3 off one VCO. The 1:1 ratio holds **independently of the `CLKFBOUT_MULT_B = 21` / `CLKFBOUT_FRACT_B = 24` encoding**, which I have not resolved; both clocks are VCO/3 whatever the VCO is. (Absolute frequency lands in the ~934–944 MHz region for either fractional interpretation, consistent with the 950 MHz class in the IP Wizard speedgrade table, but I state that as approximate and do **not** rely on it.)
2. `capture_clk` = VCO/9 = enc_clk/3, corroborating the comment.

**Consequence for E7 — the character of the question changes.** The `xil_sc_sync3` transfer at `vcu2_int_wrap.sv:4962-4969` is therefore **not a classic asynchronous CDC**. It is a same-frequency, same-PLL transfer across `BUFG_GT` + clock-mux + ICG insertion delay. So:
- A one-`sync_clk`-cycle pulse out of `xpm_cdc_pulse` is already **one encoder-clock-period wide** before it reaches the core.
- Whether it is captured as exactly one `vcu2_dfx_enc_clk` cycle — Allegro's requirement — depends on **bounded skew and timing closure**, not on structure. Under adverse skew the same pulse can be resampled as two cycles, or missed.

**This reframes E7 from a structural defect question into a timing-closure question.** That is a materially different claim and it cuts both ways: it supplies a plausible reason the EDT-1094051 edge-detect was commented out (the pulse is already the right width at the source), *and* it means the safety of removing it rests on STA margins **I have not seen and cannot check from this tree**. **No STA, simulation, elaboration or formal run has been performed by me — this remains structural reading only.**

**Standing caution, third instance:** this is again a finding that leans reassuring. The divider values are from **one BD build** of one wrapper (D2 open), the skew argument is unquantified, and `SIM_DEVICE("VERSAL_AI_EDGE_2")` at `rfs.v:687` is the only device-family datapoint in the chain — noted for D2, not treated as the target identity.

**E6-E14 provenance labelling (orchestrator request, 2026-08-12).** Distinguishing what I read from what I was told, because a blanket label would misstate both:
- **PROVER-DIRECT (read by me, this session, from the named file and line range):** E6 (Jira record via `search_xilinx_jira`), E7 (`vcu2_int_wrap.sv:2229`, `:4949-4960`, `:4962-4992`), E8 (`vcu2_v3_0_rfs.v:1372-1396` and the `vcu2_v3_0_2_lc_sync_module` body). These are direct primary-source reads, not reports, and are not downgraded to "reported".
- **PROVER-INFERRED, UNEXECUTED:** E9 — that a synchronised level cannot produce the assertion-checked single-cycle behaviour in E6. Structural inference. **No simulation, elaboration, formal run or compile has been performed by me at any point in this audit.**
- **ECHO-REPORTED / TRACE-REPORTED, UNAUDITED BY ME:** any downstream Allegro consumer behaviour on `.sync_eof` (what the IP does with a multi-cycle level), the Allegro ticket contents (5011 / 6213, off-disk), and any claim about the resolved target build configuration. I have audited none of these and they carry no weight in my reasoning until I do.

**Direction verdict remains UNKNOWN**, and is now partly superseded as a question. The `_with_fix` retention reason likewise remains **UNKNOWN** (no `p4`, D1).

**Triage needed:** (1) ~~`EDT-1094051` Jira record~~ — **answered, E6**; (2) whether `vcu2_enc_sync_eof_sync_with_clk` satisfies the requirement — **narrowed by E7/E8: not within `vcu2_int_wrap`; depends entirely on the PL-side generate configuration**; (3) `_with_fix` retention reason — **UNKNOWN**, unanswerable without source-history or owner evidence I do not have (D1); (4) **new** — the resolved values of `ONE_INSTANCE` and `LLP_MODULE` for the target build, and whether any non-LLP or one-instance configuration is a supported product configuration. Candidate classifications if pursued: `PROBABLE_REGRESSION`, `NOT_A_BUG` (supersession), `UNREACHABLE_OR_CONFIGURATION_EXCLUDED`, or `DOCUMENTATION_BUG` (misleading dead file). **I assert none of these.**

---

## 5. Candidate reconstructions

**None.** No candidates have been formally submitted. Orchestrator confirmed the candidate list is intentionally empty during independent Phase 1.

I am holding reconstruction as instructed. Nothing below this line will be written speculatively.

---

## 6. Rejected claims

**None yet** — nothing has been submitted to reject.

Recorded for future application: I have *pre-emptively* identified three rejection grounds that are live in this environment and likely to catch real candidates:

1. **RAG-sourced arch requirement** not re-verified against on-disk v1.2 (§2.1).
2. **Diablo / ZU+ VCU behavior** imported into VCU2 reasoning, especially in the seven enumerated delta areas (§1).
3. ~~**Reset or Abus Switch/DPSTx2 candidates** duplicating `EDT-1069519` / `EDT-1072959`, already absorbed into DID 1.5.1.~~ — **SUSPENDED (SENT-005 / orchestrator [HISTORY]).** This rejection ground is withdrawn and must not be used. See §2.2a.

---

## 7. Unresolved evidence dependencies

| # | Dependency | Blocks | Owner | Severity |
|---|---|---|---|---|
| D1 | `p4` binary, or a `p4 have` / `p4 changes -m1` dump for client `yadav+everest+v1+35` | Immutable RTL identity; caps all verdicts below `CONFIRMED_*` | Orchestrator / tree owner (`yadav`) | **HIGH** |
| D2 | Target release/config: device (T10/T40/T50?), silicon revision, tapeout | Checklist item 2 for every candidate | Orchestrator | **HIGH** |
| D3 | XRDB connectivity | Register ground truth | Infra | MEDIUM — mitigated by `regspecs/` |
| D4 | Jira token for `cloud_atlassian` | — | Infra | LOW — mitigated by `search_xilinx_jira` |
| D5 | Confirmation the RAG corpus should be refreshed to arch 1.2 + VCU DID | Peer agents citing stale spec | Orchestrator | MEDIUM |
| D6 | Agreed waiver/errata set in scope | Checklist item 14 | Orchestrator | MEDIUM |
| **D9** | **`regspecs/` provenance and XRDB parity unaudited.** XRDB is down and the on-disk register spec is an unvalidated substitute; no macro/generate resolution has been done (§4 "Not yet done"), and Atlas has not audited it. | Register-level ground truth is **blocked**, not mitigated. Any register-sourced candidate is unqualified until this closes | Atlas / register-spec owner | **MED-HIGH** |
| **D8** | **No post-`06-17-24` implemented netlist.** The only netlist in tree (`030_dc_dft/vcu2_core/vcu2_core.v`) is dated Jun 5 2023, pre-dates the ENC/DEC reset split, and lacks `enc_raw_rst_n`. (§4B/E5) | Blocks netlist-level confirmation that the independent reset path exists as implemented, for INV-RESET-ISOLATION-01 and any reset-adjacent candidate | PD / DFT release owner | **HIGH** |
| **D7** | **Release status of arch spec 1.2** — was it formally released, or is 1.0 still the released revision the DID correctly pins? (§2.4) | **Which architecture document is authoritative for every candidate in this audit** | Arch owner (Ygal) / release management | **HIGH** |

D2 is worth emphasizing: Confluence shows **T10, T40, and T50** VCU activity, and RAG holds separate `VNC_Test_Plan_T10_VCU.xlsm` and `T50_VnC_Test_Plan_VCU.xlsm`. These are different configurations. A defect real in one may be `UNREACHABLE_OR_CONFIGURATION_EXCLUDED` in another. Without D2 I cannot adjudicate reachability.

---

## 8. Sources searched this session

**Filesystem (read-only):**
- `/everest/apex_pvs_nobkup/karthick/vcu_analysis` — dir listing; `Everest_MMD_VCU_Arch_Specs_1.2 (1).pdf` (full text → `/tmp/pg_arch12.txt`); `Telluride_DID_1.5_VCU.docx` (full text via zipfile/XML)
- `.../shadow/` — top level, `.icmconfig`, git probe
- `.../shadow/rtl/` — listing; `vcu2_v1_7t/` full subdir map; RTL file count
- `.../shadow/verif/` — listing; `verif/vcu2_v1_7t/` map
- Version/manifest sweep across `shadow/` (depth 3)

**MCP:**
- `xrdb.xrdb_list_projects` → error
- `sage.search_docs` × 2 → "VCU video codec unit architecture", "Telluride DID VCU revision history version"
- `sage.search_xilinx_jira` → `text ~ "VCU" ORDER BY updated DESC`
- `cloud_atlassian.jira_search` → 401
- `cloud_atlassian.confluence_search` → "VCU"
- `github.get_me`

**Executed:** nothing compiled, elaborated, simulated, or formally proven. No such claim is made anywhere in this document.
