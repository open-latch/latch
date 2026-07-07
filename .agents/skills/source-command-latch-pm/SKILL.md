---
name: source-command-latch-pm
description: Seed one ruled-out decision, then show latch catch a future agent trying to revive it. Use when the user invokes $source-command-latch-pm, latch-pm, /latch-pm, or wants the Codex equivalent of Claude Code's /latch-pm command.
---

# source-command-latch-pm

Use this skill when the user wants the latch PM demo/workflow in Codex: capture
one concrete ruled-out decision and show latch's decision-continuity guardrail.

## Workflow

1. Read what latch already knows with the available latch KB read tools
   (`latch_recent`/`kb_recent`, priority listing, and search as needed). Do not
   re-ask what is already captured.

2. Tell the user in one line that you will help seed one decision latch can use
   to stop a future agent from quietly undoing their judgment.

3. Ask for one concrete approach they already ruled out and why. Good fuel is a
   specific library, framework, service, architecture, or workflow that another
   agent might plausibly propose again.

4. If the answer is vague, ask at most one sharpening question. If it is still
   too vague, fall back to a lightweight project snapshot rather than turning
   this into an interrogation.

5. Confirm before writing. Render the proposed node with kind, title, body, and
   any links. On accept/edit, write it with `latch_insert`/`kb_insert` as a
   `decision` in `staging` status and link it to the current workstream when one
   is known. Skip means no write.

6. After the decision is saved, invite the user to trigger the trip-wire by
   asking for the rejected path. For the strongest proof, suggest trying it in a
   fresh session too. When they do, run latch search/gate, cite the stored
   decision, and redirect with the saved reason.

Keep the payoff focused on decision continuity, not generic project summaries.
