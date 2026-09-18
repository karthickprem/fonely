# Sentinel Coverage Matrix

## Rules

Allowed states are exactly: `NOT_STARTED`, `IN_PROGRESS`, `EVIDENCE_PENDING`, `BLOCKED`, `INVESTIGATED_NO_ISSUE`, `CANDIDATE_FOUND`, `PROVER_REVIEW_PENDING`, `COMPLETE`.

`INVESTIGATED_NO_ISSUE` requires a concise list of inspected authoritative passages, RTL paths/lines, elaborated configuration and relevant verification evidence. Tool connectivity, filename discovery, taxonomy generation and a quick absence of findings are not investigation evidence.

Because SENT-001 is open at the Phase-1 gate, all substantive categories remain `BLOCKED` or `NOT_STARTED`. No category is terminal.

| # | Coverage category | State | Current evidence | Required next evidence / owner |
|---:|---|---|---|---|
| 1 | Requirements and XRDB traceability | BLOCKED | XRDB connection failed; release unknown | XRDB access/project/release, requirement originals and trace disposition — Ledger |
| 2 | Architecture contracts and invariants | BLOCKED | Local architecture filename v1.2 located; authority unknown | Approved/applicable original passages and invariants — Atlas |
| 3 | RTL implementation mapping | BLOCKED | RTL tree readable; immutable revision/config unknown | P4 identity/manifest, hierarchy, modules/lines, elaboration — Trace |
| 4 | Reset behavior | NOT_STARTED | None | Reset requirements, domains, RTL paths and reset sequencing |
| 5 | Clock gating | NOT_STARTED | None | Clock/gating contracts, enables, test evidence |
| 6 | Power transitions | NOT_STARTED | None | Power-state contracts, isolation/retention and sequencing |
| 7 | CDC/RDC | NOT_STARTED | None | Domain map, synchronizers, resets and CDC/RDC evidence |
| 8 | Register reset/default behavior | BLOCKED | XRDB/register authority unavailable | Applicable register spec plus RTL reset/default/W1C/W1S mapping |
| 9 | Software and firmware contracts | BLOCKED | Applicable release/config unknown | DID/SW originals, legal sequences, driver/firmware mapping |
| 10 | Start/completion | NOT_STARTED | None | Expected transitions, RTL temporal trace, observations |
| 11 | Stop/flush/drain/abort | NOT_STARTED | None | Legal scenarios and cycle-level implementation trace |
| 12 | Immediate restart | NOT_STARTED | None | Back-to-back/restart contract and cycle-level trace |
| 13 | Interrupts and status | NOT_STARTED | None | Register semantics, synchronization latency, clear/set behavior |
| 14 | AXI/DMA behavior | NOT_STARTED | None | Protocol/config authority, channel and descriptor traces |
| 15 | Buffers and descriptors | NOT_STARTED | None | Ownership/lifetime contracts, initialization and boundary traces |
| 16 | Pipeline backpressure | NOT_STARTED | None | Ready/valid propagation, stall persistence and loss/duplication checks |
| 17 | FIFO overflow/underflow | NOT_STARTED | None | Depth/elaboration, guards, simultaneous operations, proofs/tests |
| 18 | Pointer/counter wrap | NOT_STARTED | None | Widths, wrap rules, legal extrema, cycle traces |
| 19 | Frame/slice/packet boundaries | NOT_STARTED | None | Boundary contracts and first/last/empty/min/max cases |
| 20 | Multiple contexts and instances | BLOCKED | Feature configuration/instance count unknown | Context/instance configuration and isolation traces |
| 21 | Dynamic reconfiguration | NOT_STARTED | None | Legal update windows, active-job behavior and stale-state checks |
| 22 | Timeout and recovery | NOT_STARTED | None | Timeout values, recovery contracts, observable status |
| 23 | Illegal-state recovery | NOT_STARTED | None | Reachability, safe recovery/default behavior and proof/tests |
| 24 | Arithmetic width/signedness/truncation | NOT_STARTED | None | Elaborated widths, casts, extrema and reference calculations |
| 25 | Parameters/macros/generate blocks | BLOCKED | Elaborated feature configuration unknown | Active macro/parameter set and generate-path proof — Trace |
| 26 | Minimum and maximum legal values | NOT_STARTED | None | Authority for legal ranges and boundary simulations/formal checks |
| 27 | Stale state between jobs or contexts | NOT_STARTED | None | Reset/reinitialize/ownership traces across jobs and contexts |
| 28 | Historical defects | BLOCKED | Jira/Confluence/GitHub not meaningfully searched | VCU/release defect queries and original records — Echo |
| 29 | Incomplete fixes and backports | BLOCKED | Target branch/change and history unavailable | Fix commits/CLs, affected branches/releases and regression evidence — Echo/Trace |
| 30 | Verification exclusions and coverage holes | BLOCKED | Applicable verification collateral/config unknown | Testplan, exclusions/waivers, coverage reports and untested paths |

## Totals

| State | Count |
|---|---:|
| NOT_STARTED | 20 |
| IN_PROGRESS | 0 |
| EVIDENCE_PENDING | 0 |
| BLOCKED | 10 |
| INVESTIGATED_NO_ISSUE | 0 |
| CANDIDATE_FOUND | 0 |
| PROVER_REVIEW_PENDING | 0 |
| COMPLETE | 0 |

Terminal for closure (`COMPLETE`, `BLOCKED`, or evidence-backed `INVESTIGATED_NO_ISSUE`): **10/30**, but the ten `BLOCKED` categories do not represent investigated coverage and must be disclosed. Overall substantive investigation completed: **0/30**.
