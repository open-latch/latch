---
description: Seed one decision the user has already ruled out (with its reason), then show latch catch a future agent trying to revive it — latch's decision-continuity guardrail, live
argument-hint: "(no args — re-runnable any time you make or reverse a decision)"
---

This command's job is to deliver **one moment**: the user watches latch **catch an
agent about to revive a decision they already ruled out**, and cite the reason.
Optimize the whole flow for *time to that first catch* — not for producing a tidy
project summary. A summary is commoditized; any tool can turn a description into
goals and constraints. Latch's defensible moment is decision continuity: it stops
a coding agent from confidently rebuilding the thing the team already threw out.

A project snapshot is **fallback / secondary** (§7), used only when there's no good
decision to seed yet. Don't lead with it and don't present it as the payoff.

Everything is **confirm-gated** — propose, never auto-write. Use existing MCP tools
only (`kb_recent`, `kb_priority_list`, `kb_search`, `kb_insert`, `kb_priority_add`,
`kb_link`).

## 1. Read what latch already knows
`kb_recent` and `kb_priority_list` (and `kb_search` for existing decisions /
ruled-outs if the recent list is thin). Don't re-ask what's already captured.

## 2. Say what you're doing — one line
> "I'll help you seed one decision latch can use to stop a future agent from
> quietly undoing your judgment — then I'll show you it working."

## 3. Ask for one concrete ruled-out decision
> "What's one approach you've already ruled out — a **library, framework, service,
> architecture, or workflow** you considered and decided against — and **why**?"

You're hunting a decision with three properties:
1. **Concrete** enough to store as a `decision`.
2. **Plausible** an agent might propose it again later.
3. **Reasoned** — the *why* is attached. The reason is the asset; it's the part
   missing from code, docs, and every generic memory tool.

Strong: *"Ruled out Redis for the queue — using Postgres-backed jobs, don't want to
operate another service."* / *"Ruled out a cloud-hosted default — local-first trust
matters for early adoption."*
Weak: *"avoid complexity"*, *"make it good"*, *"keep it simple."*

## 4. Grade the fuel; sharpen at most once
If the answer is vague, ask **one** sharpening question and stop:
> "What's a specific path an agent might propose that you'd want latch to catch and
> challenge?"

Still weak → go to fallback (§7). Do not turn this into an interrogation.

## 5. Confirm-gate the decision
Render the exact node before writing, and **put the reason in the body** — that's
the asset:

```text
Proposed latch decision
  kind:  decision
  title: Ruled out Redis-backed queue
  body:  Ruled out a Redis-backed queue and chose Postgres-backed jobs, because we
         don't want to operate another service yet.
  links: <current workstream, if known>

Accept / edit / skip?
```

Write with `kb_insert(kind="decision", status="staging")` only on accept/edit;
`kb_link` it to a workstream if one is known. Skip = no write. (Staging is the
conservative default and is still retrievable by the real gate later; promotion to
a foundational/priority tier stays a separate, user-confirmed step.)

## 6. Arm the trip-wire, then fire it
After the decision is saved, invite the user to trigger it — and offer the stronger
cross-session version:
> "Good. Now ask me to do the thing you just ruled out — e.g. *'add a Redis queue
> for background jobs'* — and I'll catch it. **Or, to see the real clean-slate
> effect, open a new session and ask there:** a fresh agent with none of this
> conversation in its context will still find the decision and stop itself."

When they ask for the rejected path, run `kb_search`, find the stored decision, and
**redirect with the reason**:

```text
Latch caught this — the plan revives a decision you saved to latch.
You ruled out a Redis-backed queue because you don't want to operate another
service yet; the current direction is Postgres-backed jobs.
Source: KB decision <id/title>; authority: staging/current for this project.
Override that decision, or hold the line and go with Postgres?
```

Then offer the real engine as an optional follow:
> "That was a fast keyword match so you'd see it instantly. Want to watch latch's
> *actual* production gate fire on it? Run `/kb-gate \"add a Redis queue for
> background jobs\"` — a slower LLM check (~1–2 min) that searches the KB and
> returns a **Latch gate** verdict with the cited decision, rationale, source,
> and current authority."

## 7. Be proof-honest about the demo
If the catch fired **in this session**, immediately distinguish the scripted demo
from real value:
> "I triggered that on purpose so you'd see the shape now. Two things differ from
> real use: **(1)** you told me this 90 seconds ago, and **(2)** I used a fast
> keyword search, not the full gate. The real payoff is **cross-session** — a fresh
> agent next week, with none of this conversation in its context, can still find
> that decision and stop itself from quietly reviving the rejected path."

If the user instead ran the trip-wire from a **fresh session** (per §6), they just
saw the real thing — say so plainly; the "you told me 90 seconds ago" caveat no
longer applies (the fast-keyword-vs-full-gate note still does — offer `/kb-gate`).

Never overclaim that this same-session command *is* the production gate. In normal
use the catch happens when the `UserPromptSubmit` hook surfaces the decision to a
regular agent, or when that agent runs `kb_gate` — a different trigger, same outcome.

## 8. Fallback — no good ruled-out decision yet
Don't fake a trip-wire on weak fuel:
> "No problem — the catch pays off once you've got a real decision or rejected path
> worth preserving. I can seed a lightweight project brief now, and you can re-run
> `/latch-pm` the moment you've got something you want future agents not to undo."

Then optionally capture a light brief, confirm-gating each item: current goal →
`kind=workstream`; constraints / ruled-outs → `kind=decision`; unknowns →
`kind=open_question`; working-style → `kind=preference`; "how I want you to work"
standing directives → **offer** `kb_priority_add`. Frame this as *setup*, not the wow.

## 9. Close
Briefly play back what was captured. Note `/latch-pm` is re-runnable whenever a
decision is made or reversed. Optionally offer `/kb-compact` to summarize the
conversation itself into the KB.

## Guardrails
- **One concrete ruled-out decision first.** Don't open with a five-question form.
- **Confirm-gate everything; never auto-write.**
- **Adaptive depth, not an interrogation.** Batch any small supporting facts into a
  single "accept all / edit / drop" card — only after the gate attempt or in fallback.
- **Escape hatch:** if the user says *"stop interviewing,"* summarize what was
  captured so far and end.
- **Opt-in cost.** Real LLM work, acceptable because the user opted in — keep it
  tight: a question, a proposed node, a confirm.
- **Cross-platform.** Pure conversation + MCP tools; nothing OS-specific.
