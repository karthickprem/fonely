# trace_rtl.md — TRACE / rtl-tracer

Status: Pass-1 partial. INV-RESET-ISOLATION-01 resolved structurally.
All operations read-only.

---

## 0. Provenance (CORRECTED — supersedes my earlier wording)

I previously wrote "LOCAL MODIFICATIONS: PRESENT — tree is not clean vs its sync."
**That was overstated and is withdrawn.** Correct statement:

> Some files carry mtimes newer than a hypothesized 2026-03-12 snapshot date.
> Modification status and workspace cleanliness are **unprovable** from this host.

Evidence that P4 identity is unobtainable here:
- `shadow/.icmconfig` (53 B) = `P4CLIENT=yadav+everest+v1+35`, `P4PORT=xhdicmsuper:1777`. No changelist.
- No `p4` binary on host.
- `shadow/rtl/p4integs.log`: the workspace's own `Reference::GetWsTag` failed — `User yadav doesn't exist.`

Target release / SKU / stepping / config applicability remain **BLOCKED**.

### Byte-identity manifest (NOT provenance)

Algorithm (`vcu_audit/resolve_filelist.py`, read-only):
- Root filelist `rtl/vcu2_v1_7t/vcu2_v1_7t.filelist`; `$RTL_ROOT` = `shadow/rtl`.
- Recursive `-f` expansion with a visited-set cycle guard; re-entries recorded as duplicates, parsed once.
- `//` = comment (incl. trailing). Leading `#` = disabled directive, skipped.
- `[section]`/`[/section]` scoping. `-y` → libdirs. Other `-*` ignored.
- `[include]`/`[includey]` → +incdir list (recorded, never hashed).
- `[mbist]` recorded, **excluded** from the identity digest.
- Digest set = `[header] ∪ [synth] ∪ [behv]`, deduped, `sorted()`, existing regular files only.
- Manifest line = `sha256(file)  relpath_to_rtl_root`; identity = sha256 of that body.

Results:
| metric | value |
|---|---|
| filelists parsed | 67 |
| duplicate `-f` (parsed once) | 49 |
| `[synth]` / `[behv]` / `[header]` entries | 1990 / 169 / 9 |
| `[library]` / `[mbist]` / `[testbench]` | 138 / 610 / 5 |
| unique existing design files hashed | **2166** |
| **MANIFEST SHA-256** | `5ec3e8a5748b78ce5d864a8ce928d0a0d693a62cb6d2a1bf219e2db3acbd3d53` |
| missing refs | 375 (mostly `top_7t_n1_common.filelist` incdirs for other IP blocks + one `$SW_ROOT` synth entry) |

Disclosed exclusions: `[mbist]`, `[library]`, `[include]`, `[testbench]`, glob entries
(`tb/*.sv`), and `$SW_ROOT`-rooted entries (variable unset).
**This is analyzed-byte identity only. Not a changelist, not cleanliness, not
"the compiled fileset" — no elaboration/build evidence has been obtained.**

---

## 1. INV-RESET-ISOLATION-01 — RESOLVED (structural)

**Verdict: the independent HW resets EXIST and are functionally wired in the
active compiled hierarchy. Encoder and decoder raw-reset cones are disjoint.**

Echo's P2 ("closed SW-only, fix cosmetic") is **contradicted by the RTL**.

### 1.1 Source → consumer cone

`shadow/rtl/vcu2_v1_7t/vcu2_core_top/rtl/vcu2_core_top.sv`
```
 353:  input [127:0] pl_vcu2_spare_in,
1010:  wire pl_vcu2_enc_raw_rst_n;
1011:  wire pl_vcu2_dec_raw_rst_n;
1013:  // PD recommendantion is to use [56] for ENC and [11] for DEC reset inputs.
1014:  assign pl_vcu2_enc_raw_rst_n = pl_vcu2_spare_in[56];
1015:  assign pl_vcu2_dec_raw_rst_n = pl_vcu2_spare_in[11];
1018:  vcu2_reset i_vcu2_reset(
1021:    .pl_vcu2_raw_rst_n     (pl_vcu2_raw_rst_n),
1024:    .pl_vcu2_enc_raw_rst_n (pl_vcu2_enc_raw_rst_n),
1025:    .pl_vcu2_dec_raw_rst_n (pl_vcu2_dec_raw_rst_n),
1033:    .vcu2_enc_rstn         (vcu2_enc_rstn),
1034:    .vcu2_dec_rstn         (vcu2_dec_rstn),
```
Both bits are stripped from the downstream spare bus so they are not double-used:
```
1358:  .pl_vcu2_spare_in ({pl_vcu2_spare_in[127:57], 1'b0,
                          pl_vcu2_spare_in[55:12],  1'b0,
                          pl_vcu2_spare_in[10:0]}),
```

`shadow/rtl/vcu2_v1_7t/vcu2_reset/rtl/vcu2_reset.sv` (101 lines, purely combinational std cells; comment `//abhayg-06-17-24: Add seperate resets for ENC and DEC` at :4, :65, :79):

```
common = pl_vcu2_raw_rst_n & ~ipor_pcsr & ~pcsr_initstate & init_vcu2_por_b     (:31–50)
vcu2_interconnect_rstn = common & (ssc_fuse_valid_enc_enable | ..._dec_enable)  (:52–62)
vcu2_enc_rstn = (common & ssc_fuse_valid_enc_enable) & pl_vcu2_enc_raw_rst_n    (:67–77)
vcu2_dec_rstn = (common & ssc_fuse_valid_dec_enable) & pl_vcu2_dec_raw_rst_n    (:81–91)
vcu2_bisr_dfx_npislave_rstn = ~ipor_pcsr & init_vcu2_por_b                      (:93–97)
```
Polarity: active-low throughout. `spare_in[56]=0` asserts ENC reset; `[11]=0` asserts DEC reset.

**Independence proof:** `pl_vcu2_enc_raw_rst_n` fans out to exactly one gate
(`i_xil_sc_and2_reset_6`, :73) whose only sink is `vcu2_enc_rstn`.
`pl_vcu2_dec_raw_rst_n` → `i_xil_sc_and2_reset_7` (:87) → `vcu2_dec_rstn` only.
Neither touches `vcu2_interconnect_rstn` or the other engine. **Disjoint cones.**

### 1.2 Answer to Atlas's three-way question
- **ENC-only reset net exists**: yes — `vcu2_enc_rstn` via `spare_in[56]`.
- **DEC-only reset net exists**: yes — `vcu2_dec_rstn` via `spare_in[11]`.
- **Shared net also exists**: yes — `pl_vcu2_raw_rst_n`/ipor/initstate/por_b reach
  both through `common`. That is the intended tile-level reset per the DID.
- **Spare pins tied off?** No. Live assigns at `vcu2_core_top.sv:1014–1015`.

### 1.3 Disposition of Echo's / Sentinel's BLH hazards — both are NON-FINDINGS

`vcu2_core_top/rtl/blh/` is **generated black-box collateral, not compiled source**:
- `grep -rn 'blh' shadow/rtl/vcu2_v1_7t --include=*.filelist` → **0 hits**.
- `/blh/` paths in the 2166-file manifest → **0**.
- `vcu2_atom_if_wrap` is not instantiated anywhere in `.sv`/`.v` outside `blh/`.
- Directory also holds `blh_composer.log`, `verific.log` — composer output.

`SHIP_VCU2_VCU2_vcu2_atom_if_wrap.v:728` "`[11]` defaults 0" is **not anomalous**:
- `:54` declares `input [31:0] PLVCU2SPAREIN` — only 32 bits, so **bit 56 has no port
  in that wrapper at all**; it is a narrower atom-level view, not the block hierarchy.
- All **32/32** hookups carry default `"0"` uniformly. `[11]` is not special.
- By contrast `SHIP_VCU2_VCU2_vcu2_core.v:364` is `[127:0]` with every bit `"DoNotCare"`.

The `hookup` module has **no definition anywhere in this tree**, so the second
parameter's semantics are undocumented here. Do not read `"0"` as a tie-off
without authoritative composer documentation.

`tb_vcu2_core.sv` VDB-1002 (`pl_vcu2_raw_rst_n` no driver): **not confirmed** —
grep for `pl_vcu2_spare_in|pl_vcu2_raw_rst_n` in `vcu2_v1_7t/tb/*.sv` returned
nothing. Unverified; needs the exact tb path from Echo.

---

## 2. Observations recorded (NOT promoted — no candidate claim)

**O1 — no reset synchronizer between PL pin and the reset AND-gates.**
`vcu2_reset.sv` is combinational only. `vcu2_enc_rstn`/`vcu2_dec_rstn` therefore
**deassert asynchronously** with respect to every consumer clock.
Downstream `vcu2_int_wrap.sv` does add 3-flop scan-controlled synchronizers
(`xlnx_rstn_sync_scan_cntrl_active_low #(.LEVELS(3))`) at :2416/:2428/:2440 (ENC,
enc_clk/axi_slave_clk/npi_clk) and :2456/:2470/:2482 (DEC) — but those feed only
interfaces and APMs (`:2453–2454`: *"Decoder Reset synchronizer only for APMs; DEC_TOP
(L1 block) has its own reset synchronizer"*).
The **hard-IP core reset pins get the raw net**:
- `:2274` `.vcu2_enc_core_rstn (vcu2_enc_rstn)` — inside `generate for (genvar i=0;i<2;i++) : g_core_num` (:2267)
- `:2031/:2045/:2048/:2060` `.vcu2_dec_arstn/_mrstn/_prstn/_rstn (vcu2_dec_rstn)`

Whether the Allegro hard IP resynchronizes internally is **Allegro-internal and
not visible from this tree** (obfuscated VHDL). Ownership: AMD integration for the
net, Allegro hard-IP for the internal handling. This is the correct question for Chronos.

**O2 — no scan bypass on the direct-to-core path.** The synchronizer instances take
`scan_en`/`scan_rst_byp`/`scan_mode_rst_n`; the raw path at :2274 and :2031–2060 takes
none. A PL pin can therefore hold an engine in reset independently of scan control.

**O3 — both encoder cores share one reset.** The `g_core_num` loop instantiates two
`vcu2_enc_core_top` with the same `vcu2_enc_rstn` — the two ENC cores are not
independently resettable. Arch invariant only requires ENC-vs-DEC separation, so this
is **not** a violation; recorded for completeness.

**O4 — common-mode axes remain shared by design.** `pl_vcu2_raw_rst_n`, `ipor_pcsr`,
`pcsr_initstate`, `init_vcu2_por_b` reset both engines via `common`. Isolation holds
**only** on the two dedicated spare pins. Per DID this is intended.

**O5 — `vcu2_reset.sv` coding hygiene.** Outputs at :14–17 are untyped (`output
vcu2_interconnect_rstn`); internal nets `reset_raw_iporpcsr`,
`reset_raw_iporpcsr_initstate`, `reset_raw_iporpcsr_initstate_porb`,
`ssc_fuse_valid_enc_dec_enable_orgated` are never declared. These elaborate as
implicit 1-bit wires — behaviourally benign, lint-relevant only.

**O6 — `#-f .../vcu2_rst_sync.filelist` is disabled** in
`vcu2_core/rtl/vcu2_core.filelist`. The file `vcu2_enc_core_top/rtl/vcu2_rst_sync.filelist`
(147 B) exists on disk but is commented out. Relationship to O1 not yet established.

---

## 3. Pre-candidate hypotheses (demoted per SENT-002 — provenance hazards only)

- **C1** `vcu2_enc_top.filelist:41` puts `vendor_ip/Xilinx_E200E_RTL_0p6/delivery/vhd_obf/`
  on the `[include]` search path while all compiled vendor source is `1p4`.
  +incdir only; no compiled file. Search-order winner **not yet resolved**.
- **C2** `vcu2_core.filelist` `-f`s `vcu2_atom.filelist` twice. Resolver parses once;
  49 such duplicates exist tree-wide, so this is idiomatic, not anomalous. Tool
  semantics still unproven.
- **C3** mtime skew — see §0. Not a defect without a P4 baseline.

None of these is a candidate. None enters Prover flow.

## 4. Rejected
- "EDT-1069519 closed SW-only; independent HW reset absent" — **rejected**, §1.1.
- "`atom_if_wrap` ties `[11]` to 0, disabling DEC raw reset" — **rejected**, §1.3.
- "ENC and DEC share one hard reset net" — **rejected**, §1.1.

## 5. Blockers
- Target SKU / stepping / release applicability — BLOCKED (§0).
- XRDB MCP unreachable — no machine cross-check of register offsets/reset values.
- Allegro hard-IP internals opaque (obfuscated VHDL) — O1 cannot be closed statically.
- `hookup` module semantics undocumented in-tree.
- `tb_vcu2_core.sv` path from Echo needed to check VDB-1002.

## 6. Proposed evidence
1. Elaborate `vcu2_core.filelist` closure; capture the include-resolution log → closes C1/C2.
2. Mine `rtl/vcu2_v1_7t/vlint.vlint.log.3448895` (187 KB, on disk, unmined) for real
   elaboration warnings on the reset nets.
3. Directed sim: with tile out of reset and both engines streaming, force
   `pl_vcu2_spare_in[56]=0` for N cycles; assert DEC AXI traffic and `vcu2_dec_rstn`
   are undisturbed. Repeat with `[11]`. This is the behavioural half of §1.1.
4. RDC tool run on `vcu2_enc_rstn`/`vcu2_dec_rstn` → quantifies O1.

## 7. Sources searched
`shadow/.icmconfig`; `shadow/rtl/p4integs.log`; 67 filelists under `rtl/vcu2_v1_7t`;
`vcu2_core_top.sv` (2530 L); `vcu2_reset.sv` (101 L); `vcu2_int_wrap.sv` (265 KB);
`vcu2_core.sv`; `vcu2_atom.sv`; `blh/SHIP_VCU2_VCU2_vcu2_core.v`;
`blh/SHIP_VCU2_VCU2_vcu2_atom_if_wrap.v`. XRDB attempted, unreachable.
`model_tags.cfg` / `.project_config` **not found** under `shadow/` at depth ≤4 —
Atlas's metadata citation is **not independently reproducible** from this path;
exact absolute path required.
