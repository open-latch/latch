# Outcome Measurement Contract v2.6 (ratified semantics) + Acceptance Matrix

**Status:** post-ratification bounded pass, updated for the 4118, 4139, 4151, and 4163 returns. Semantics frozen by founder rulings **4113** ("accept all": S1–S4, A, B, D, E, F) and **4137** (straddle membership; pin structure A). No semantic discretion exercised; §7 records each formerly-derived point's resolution. Implementation remains blocked until the reviewer's mechanical verification of this capture.
**History:** v1→v1.4 (reviews 4083/4092, self-checks 4090), v2.0–v2.1 (simplification per 4095; self-check), review 4110 → tripwire → founder batch ruling 4113 → v2.2 → capture audit 4118 (two-item return) → founder ruling 4137 → v2.3 → mechanical check 4139 → v2.4 → capture 4142 cleared, §7-4 confirmed entailed, one integration remaining (4151) → v2.5 → mechanical check 4163 (freshness/order-key alias; supersession lifecycle) → **v2.6**. Self-contained; no normative content lives in prior versions.

---

## 0. Ratified semantics (4113 — binding, restated normatively)

- **S1:** O2 definitive-fail arithmetic uses **`D_min` = proven-distinct invocations only**. Unmatched artifacts, malformed regions, and unresolved conflicts are **loss markers**: they force `indeterminate` (block pass) and never inflate fail.
- **S2:** O3 is diagnostic-only unless O2 passes. Known boundary uncertainty **censors** the affected row. **V1 green ≡ N=30 ∧ O1 pass ∧ O2 pass ∧ O3 pass.**
- **S3:** "Zero observed loss" is an **operational** V1 criterion only — not channel independence, not completeness, not an unconditional public coverage claim. No third invocation manifest.
- **S4:** Bounded-pass discipline; immutable capture pins contract, ratifications, protocol/source versions, and fixture/golden-pack hashes (fixture hashes in a linked follow-up node at implementation completion, before T0).
- **A:** O3 threshold ≤6 of the first 30 eligible; failure ⇒ repair/re-pin the instrument, G0 stays open. **B:** cap 21 days, no automatic extension; `insufficient-n` returns to the founder. **D:** the 27 hash-recovered rows are pilot. **E:** corpus-derived-fixture discipline is workstream priority 4114. **F:** no G0 amendment; G0 closes only when V1 is green.
- **4137-1 (straddles):** any-source **admission** — in U iff **any** available source timestamp is in-window; the total order `(lineage_order_key, nonce)` — lineage-stable min over ever-observed timestamps — sequences **admitted** invocations only (T1, prefix, fold) and never decides membership. Source loss can only widen the cohort; admission and order position are monotone across generations (4151). **4137-2 (pins):** structure A — node 4115 (superseded by this capture's node) is the immutable semantic base; a linked pre-T0 **manifest node** pins implementation commit, protocol version, exact source roots, fixture/golden hashes, and a composite hash.

## 1. Observations, invocations, receipts

**Observation** = one record in one source, with a source-qualified id `obs_id = (source, file, byte_offset)` — total identity even for malformed or nonce-less records. Fields when parseable: `nonce?`, `ts?`, session identity, attestation, protocol version, project check.

**Invocation** = the deduplication of observations: observations sharing a nonce are one invocation; an observation with no nonce can never be proven distinct and therefore **never becomes an invocation** — it is a **loss marker** (S1).

**Receipt** = one per invocation, identity **`(invocation, measurement_protocol_version)`** — a protocol bump reprocesses prior generations into new receipts rather than deduping them away (M5); within one version a receipt is immutable once finalized.

**Admission and ordering are separate (4137-1, per the 4139 correction):**
- **Admission (membership) predicate (4151 integration):** `admitted = currently_admitted_by_any_source OR admitted_in_a_prior_generation` — an invocation is admitted to U iff **any available source timestamp** lies in `[T0, cap)`, **or** its receipt lineage shows admission in a prior generation. The window sentinels belong to this predicate only — timestamp tests, never order positions. Single-source straddlers whose one surviving timestamp is in-window are admitted; lineage-admitted invocations stay admitted even with every in-window source lost.
- **Ordering of admitted invocations (4151 integration):** the order key is **lineage-stable**: `lineage_order_key = min(previous_lineage_order_key, currently_available_source_timestamps)` — first generation uses the earliest available source timestamp; every later generation takes the min of the inherited key and what is currently observable, so losing the earliest source can never move a still-admitted invocation later in the order or past T1 out of the measured prefix. Total order = `(lineage_order_key, nonce)`; entities lacking both keys sort last by `obs_id`. Order position never revokes admission.
- **Admission and order are monotone across generations:** admission evidence and the lineage order key are part of the receipt lineage; reprocessing (M5) re-admits every previously admitted invocation at a key no later than before. Post-admission source loss degrades evidence (a loss marker, pressing O2 toward `indeterminate` per S1) — it never ejects and never demotes prefix position. *(Entailment confirmed by the reviewer, 4151.)*

Loss markers are non-denominator entities: a marker is **in scope for a window iff its source file's date range overlaps it**; unplaceable markers are treated as in-scope (conservative).

**Required-field map** (parseable S1 row missing a field ⇒ named loss sub-reason): `nonce`/`ts` ⇒ `identity_missing` (marker — distinctness unprovable) · `attestation` ⇒ `attestation_missing` · `project/key id` ⇒ `project_proof_missing` · `protocol version` ⇒ `version_missing`. Unparseable row/region ⇒ `schema_invalid` (unparseable-only). Verdict id-list fields are **not** schema fields — their absence is caught exclusively by O1.

**Classification of invocations** (frozen ordered procedure, first match wins):

| Order | Disposition | Rule | In `D_min`? |
|---|---|---|---|
| 1 | `foreign_project` | **positively proven** foreign: valid key epoch, present-and-different project fingerprint | no — count published; exits before any target-local completeness accounting (unknown/missing project proof is **loss**, order 3) |
| 2 | `conflict` | same nonce in two comparably-projected sessions; any additional non-identical join candidate for a resolved nonce; joined records disagreeing on any shared field (`ts` tolerates 300s; all others equal) | yes (identity proven) — blocks pass; forces `indeterminate` only when fail does not hold |
| 3 | `loss_signal` | invocation-level loss with proven identity: `gate_only` (nonce'd gate row, no host record) · `host_only` (nonce'd host record, no gate row) · required-field sub-reasons above · `key_epoch_mismatch` | yes — blocks pass |
| 4 | `skipped` | gate row marked skipped, otherwise sound | no — count published; still bounds its session |
| 5 | `pilot` | weaker evidence: pre-nonce, hash-annotated (the 27 frozen rows only — no hash-join code ships), legacy project key, non-pinned runtime or protocol (`version_mismatch`) | yes |
| 6 | `confirmatory` | dual-source proven: schema-valid gate row + host record, nonce-joined, pinned runtime and protocol, project ok | yes |

Ingestion never filters; every observation yields an observation record; key rotation is `loss_signal(key_epoch_mismatch)`, never foreign. Whatever its disposition, an invocation's existence in a session stream still bounds that session's windows.

## 2. Sources, merge, health

- **S1 (gate log)** and **S2 (host records)** are symmetric: fixed roots pinned at T0; zero silent skips; per-source health receipts count missing files, unreadable files, malformed lines/regions. Codex rollouts and Claude transcripts both record the gate's tool result (nonce included), so dual-source is uniform across hosts. Multi-segment S2 unions by `(adapter, project, session_id)`, ordered `(ts, segment_path, line_index)`, self-exclusion; identical duplicates coalesce, non-identical ⇒ `conflict`.
- **Daily-file rule** (derived, §7): a missing daily S1 file is a loss marker **iff** S2 shows same-project activity that day; dual silence is not a marker. Symmetrically for S2 when S1 shows activity.
- **Snapshots:** full-file SHA-256 over all parsed bytes; "changed" = differs between the two passes of one double-enumeration; changed files rescan (3 retries) else the receipts being finalized from this snapshot become loss-marked. Receipts finalized from an earlier qualifying snapshot are immutable; later file growth is expected, not a defect.
- **Freshness (4163 correction):** a receipt finalizes only from a snapshot **taken** ≥ `fresh_ts_i + 1800s + 300s`, where `fresh_ts_i` is the invocation's **currently-available earliest source timestamp, recomputed at each run** — an independent quantity, **not** the lineage-stable order key. The two coincide in the common case but diverge after source deletion (the order key can be earlier, carrying a since-lost timestamp via the lineage `min`); freshness deliberately uses the current-observation value so the wall-clock drain is measured against evidence that still exists. Cached earlier snapshots never qualify.
- **Event time (founder-ratified, 4137-1 + 4151):** admission and ordering exactly as defined in §1 — §2 defines no competing rule. The authoritative (gate-row-preferred) `ts` is a display/join field only. Freshness uses its own `fresh_ts_i` (above); the ≤300s join tolerance means any cross-source delta is absorbed by the existing 300s slack. No claim of equivalence between `fresh_ts_i` and the lineage order key is made.
- **Key epoch** pinned at T0, recorded per receipt.

## 3. Boundary, observability, classification (confirmatory receipts)

- **Boundary** = next same-session invocation, from the **union** of S2 same-session records and S1 rows carrying exact session identity; skipped invocations bound; host EOF is never session end; a foreign or unknown-session call never bounds.
- **Boundary-capable loss censors (S2 ruling):** a `gate_only` invocation (or any loss marker with session evidence) inside a session that has measured windows makes those windows' boundaries unknowable — the affected rows emit `CENSORED` / `boundary_uncertain`. O3 is therefore **not** claimed inventory-immune.
- **Observability:** `observed` (zero-width windows are observed) or `unavailable` ⇒ `CENSORED` / `instrument_unavailable`, every verdict alike. Censored-confirmatory sits in `D_min` but not E — an instrument that cannot see is a coverage problem.
- **Evidence:** all classifier inputs same-session-scoped; global update count removed (deliberate semantic change, this version); failed evidence query ⇒ `CENSORED` / `evidence_unavailable`.
- **Labels and table:** `ACCEPTED` · `OVERRIDDEN` · `AMBIGUOUS` · `UNRESOLVED` · `CENSORED`; blind (observed-and-saw-nothing) stays `AMBIGUOUS`, in the rate.

| Verdict | Same-session KB evidence | touches > 0 | touches = 0 |
|---|---|---|---|
| PROCEED | progress insert > 0 | ACCEPTED | ACCEPTED |
| PROCEED | none | AMBIGUOUS¹ | AMBIGUOUS (blind) |
| MODIFY | insert links to cited id | ACCEPTED | ACCEPTED |
| MODIFY | inserts, none link | OVERRIDDEN | OVERRIDDEN |
| MODIFY | no inserts | OVERRIDDEN | AMBIGUOUS² (blind) |
| DO_NOT_PROCEED | progress insert > 0 | OVERRIDDEN | OVERRIDDEN |
| DO_NOT_PROCEED | none | ACCEPTED¹ | ACCEPTED |
| NEEDS_HUMAN_JUDGMENT | same-session insert or cited-edge activity | ACCEPTED | ACCEPTED |
| NEEDS_HUMAN_JUDGMENT | none | UNRESOLVED | UNRESOLVED |
| none / unparseable verdict | — | AMBIGUOUS | AMBIGUOUS |

¹ Touches deliberately unconsulted; changing this is semantic ⇒ bump ⇒ restart. ² Founder ruling 3985.

## 4. Window, oracles, verdicts

- **T0** set at pre-registration after: merge + install verified; protocol version and key epoch pinned; per-host nonce canaries (Codex **and** Claude Code) each live-proving tool-result → gate log → host record → dual-source join; ratifications recorded (done: 4113). Admission per §1's predicate; ordering per §1's total order (4137-1). T1 = the 30th-eligible-outcome invocation in admitted order, else the cap; the prefix = admitted invocations up to and including T1's position; pre-T0 sessions contribute boundary evidence only. Cap 21 days, **no automatic extension** (B). Audit runs post-drain (`close + 1800s + 300s`, fresh snapshot). **Hard-invalidation precedes arithmetic:** impossible stored combinations, conflicting duplicate content, or a tamper-check failure invalidate the audit before any oracle is computed.
- **O1 — field presence:** over **every parseable in-window gate row, regardless of disposition, attestation, or pinnedness**: 100% carry the verdict id-list fields, each validated as a list of ints. (Runtime attribution of a violation is diagnostic, not a filter.)
- **O2 — coverage** ∈ `pass` | `fail` | `indeterminate`, computed over the window universe:
  - `E` = eligible (confirmatory ∧ outcome ≠ CENSORED). **`D_min`** = proven-distinct invocations: `confirmatory + pilot + loss_signal + conflict` (all identity-proven; `skipped` and `foreign_project` excluded, counts published). Loss **markers** are not in `D_min`.
  - **Fail:** `10·E < 7·D_min` ⇒ fail — definitive, because `D_min` is the smallest defensible denominator and real loss could only lower true coverage further.
  - **Pass:** `10·E ≥ 7·D_min` ∧ zero loss signals ∧ zero loss markers ∧ zero conflicts ∧ source-health receipts clean ⇒ pass (the S3 operational proxy, so stated).
  - **Otherwise `indeterminate`** with reasons published. Worked example (the 4110 counterexample): E=30 plus seven broken calls each observed as a nonce'd `gate_only` and a nonce-less `unjoinable`: `D_min = 37` (markers excluded), fail condition `300 < 259` is false, markers present ⇒ **indeterminate** — not the false fail v2.1 produced.
- **O3 — AMBIGUOUS:** ≤6 among the first 30 eligible outcomes (A). Diagnostic-only display when O2 is not passing; contributes to green only with O2 pass (S2).
- **V1 green ≡ N=30 ∧ O1 ∧ O2 ∧ O3** (S2). `insufficient-n` at cap: O3 unevaluated, O1/O2 published, verdict neither pass nor fail, decision returns to the founder (B).

## 5. Acceptance matrix

Fixtures corpus-derived per priority 4114; sanitized fixtures preserve encoding byte-for-byte; local-only conformance floors run against the unsanitized corpus.

B1 golden end-to-end (observations → receipts → rendered report, byte-identical) · B2 per-host live canaries · B3 reconciliation (deleted gate line ⇒ `host_only` + indeterminate; malformed ⇒ counted marker; conflicting nonce ⇒ conflict) · B4 foreign-project vs non-pinned-runtime vs missing-attestation, separately · B5 freshness incl. cached-snapshot variant · B6 foreign-session call never truncates; skipped same-session call truncates, emits no outcome, its receipt counted · B7 rate integrity (M3-scoped) · B8 oracle arithmetic: **the 4110 false-fail case asserts `D_min=37` ⇒ indeterminate, never fail**; O2 edges |U|=42/43 at N=30; O3 edges 6/7; O1 catches a field-less row of any disposition · B9 key rotation ⇒ in-`D_min` loss + indeterminate, never foreign · **B10** source-deletion straddles at T0 and T1, both directions — **admission, lineage order key, prefix position, and loss evidence all survive post-admission removal of either source** (incl. the earliest-timestamp source); and after such deletion `fresh_ts_i` (current) and the lineage order key are asserted to legitimately differ, with freshness computed from the current value · **B11** gate-only same-session invocation censors the affected measured windows (the 391s class, now on the boundary side) · **B12** proven-foreign single-source records exit before completeness accounting and cannot poison O2 or M1 · **B13** required-field cross-product: each missing field ⇒ its mapped sub-reason, exhaustively · **B14** full-file middle rewrite caught by snapshot hash · **B15** malformed region at the T1 edge remains an in-scope marker and forces indeterminate · **B16** ts join edges at 300s/301s · **B17** generation isolation: same invocation across protocol versions yields distinct receipts, audit reads pinned only · **B18** tamper: capture-hash mismatch or supersession without a new node invalidates the audit.

Metamorphic: **M1** adding unrelated **non-measured candidate sessions** changes no attribution, boundary, signal, outcome, or clean metric · **M2** adding a competing candidate **present before qualifying finalization** degrades `confirmatory` → `conflict`, never upgrades · **M3** adding non-boundary-capable diagnostic/pilot receipts leaves clean quality stats and prose bit-identical while D-side counts change exactly as specified · **M5** protocol bump reprocesses, never dedups away.

## 6. Capture manifest and status

- **Structure A (founder-ratified, 4137-2):** the capture node holding this text's SHA-256 is the **immutable semantic base** (superseding 4115, which is ratified as the base lineage). One linked **pre-T0 manifest node** — `depends_on` from the base, required before T0 — pins: implementation source commit · measurement protocol version · exact source roots · fixture/golden-pack hashes · a **composite SHA-256** over (contract hash ‖ ratification ids ‖ commit ‖ protocol version ‖ roots ‖ fixture hashes). T0 is mechanically impossible without the manifest; nothing runs unpinned.
- Capture nodes are never updated; correction = new node + `supersedes` edge; the audit verifies the pinned hashes at close (B18).
- **Blocked until:** the reviewer's mechanical verification that this capture matches rulings 4113 + 4137. Then implementation; then fixtures + manifest node; then per-host canaries; then T0.

## 7. Formerly-derived points — resolution record

1. **Straddle inclusion:** the reviewer correctly ruled this a semantic choice, not an entailment; **founder-ratified** as any-source membership with the single total order (4137-1). No longer a derivation.
2. **Daily-file manifest rule:** reviewer-verified as entailed by S1/S3 (4118).
3. **Fixture-hash sequencing:** reviewer-verified as authorized by S4 (4118); the previously-undisclosed deferral of protocol-version and commit pins is explicitly covered by ratified structure A (4137-2).
4. **Monotone admission across generations** (§1): reviewer-confirmed as entailed by 4137-1 (4151 — "no new founder decision is required"), with the two integration rules folded in verbatim: the lineage-OR admission predicate and the lineage-stable min order key. No open derivations remain.
