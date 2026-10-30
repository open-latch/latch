"""Isolated, pinned elicitation for the adherence harness (KB id=5134).

Review round 1 (id=5217) failed the first version on three counts that all live
in this one seam, so it now has its own module rather than borrowing latch's
internal model path:

* **The pin was recorded but not applied.** ``model_backends.invoke_prompt``
  takes env-var *names*, not a model value, so the explicit ``--model`` the
  protocol requires never reached any CLI while the artifact reported
  ``model_pin_source: explicit``. A pin you cannot see in the argv is not a
  pin, so every call now records the exact argv it ran.
* **The answer was never unwrapped.** ``claude --output-format json`` returns
  an envelope; the grader expects the bare ``DECISION:`` envelope and scored a
  perfectly good answer ``UNPARSEABLE``, which fails closed into "not
  adherent" and would have silently depressed whichever arm ran on Claude.
  ``compactor._parse_json_envelope`` already solved this; the same shape is
  applied here.
* **The child was not isolated.** A bare ``claude -p`` inherits the ambient
  cwd, the user's settings, and the full tool surface including latch's own
  MCP server — so the protocol's "single call, no tool use" was an assumption,
  not a property, and a child could have reached the live vault where the
  parent-process SQLite tripwire cannot see it.

Isolation is therefore structural and per-backend:

* cwd is the arm's own scrubbed workspace — never the operator's repo — so
  ambient project files cannot leak into any arm and the scrub becomes
  load-bearing for all three arms rather than decorative for two.
* the tool surface is denied and MCP is switched off, so latch's own tools are
  unreachable from the child.
* latch's pin variables are stripped from the child environment and
  ``LATCH_UNLATCHED=1`` is set, so even a latch process started inside the
  child cannot resolve the production vault. This is defence in depth for the
  one gap the tripwire structurally cannot cover: it patches ``sqlite3`` in
  *this* process only.

RESIDUAL LIMITATION, stated rather than papered over: a child process is
outside the tripwire. The guarantees above are enforcement at the boundary
(deny tools, no MCP, unlatched, no pin) rather than observation, so a scored
run's non-contamination evidence for the live path rests on those flags plus
the pre/post vault digest — not on the tripwire.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from latch.mcp import mcp_runtime


SUPPORTED_BACKENDS = ("claude", "codex", "cursor")

#: Backends whose CLI accepts an explicit model id. Protocol §4.1 requires one
#: for a scored run on these; `claude` resolves host-side unless --model is
#: given, and it is given here whenever the operator supplies one.
PINNABLE_BACKENDS = ("claude", "codex", "cursor")

#: Denied for the Claude child. The protocol elicits a *plan*, not an
#: implementation: one call, no tool use. Left ambient, the child could read
#: the operator's real repo — or reach latch itself — and the arm would no
#: longer be the arm.
CLAUDE_DENIED_TOOLS = (
    "Bash", "Read", "Write", "Edit", "MultiEdit", "NotebookEdit",
    "Glob", "Grep", "WebFetch", "WebSearch", "Task", "TodoWrite",
    "SlashCommand", "BashOutput", "KillShell",
)

#: Stripped from every child environment. These are the variables that select
#: a vault or an install root; a child that inherits them can find the live
#: vault, which is the one thing a harness run must never be able to do.
_STRIPPED_ENV = (
    "LATCH_KB_DIR", "CLAUDE_KB_DIR", "LATCH_HOME", "CLAUDE_KB_HOME",
    "LATCH_TEST_ROOT", "LATCH_TEST_CAPABILITY", "LATCH_PRODUCTION_DATA_ROOT",
    "LATCH_VAULT_REGISTRY_ROOT", "LATCH_ADHERENCE_SCRATCH",
    "CLAUDE_KB_IN_COMPACT", "LATCH_VAULT",
)

_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\n(.*?)\n?```$", re.DOTALL)

_VERSION_CACHE: dict[str, str | None] = {}


class ElicitError(Exception):
    """The elicitation could not be configured."""


@dataclass(frozen=True)
class Elicitation:
    """One elicitation result, with the evidence that its pin was applied."""

    text: str | None
    error: str | None
    timed_out: bool
    backend: str
    backend_version: str | None = None
    #: The exact argv that ran. This is what makes the model pin auditable
    #: instead of asserted — review round 1 caught a recorded pin that was
    #: never passed to any process.
    argv: list[str] = field(default_factory=list)
    raw: str | None = None


def isolated_child_env() -> dict[str, str]:
    """A child environment that cannot resolve a latch vault."""
    env = dict(os.environ)
    for name in _STRIPPED_ENV:
        env.pop(name, None)
    # Belt and braces: if latch code runs in the child anyway, it is off.
    env["LATCH_UNLATCHED"] = "1"
    return env


def backend_version(binary: str) -> str | None:
    """`<bin> --version`, cached. Protocol §4.1 records it per run."""
    if binary in _VERSION_CACHE:
        return _VERSION_CACHE[binary]
    try:
        proc = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=30,
        )
        value = (proc.stdout or proc.stderr or "").strip().splitlines()
        _VERSION_CACHE[binary] = value[0] if value else None
    except (OSError, subprocess.SubprocessError):
        _VERSION_CACHE[binary] = None
    return _VERSION_CACHE[binary]


def unwrap_envelope(raw: str | None) -> str | None:
    """Return the model's actual answer from a CLI wrapper.

    ``claude --output-format json`` and ``cursor --output-format json`` both
    wrap the text in a JSON envelope. Grading the envelope scores a good
    answer UNPARSEABLE, which fails closed into "not adherent" — so this runs
    before the grader ever sees the text. Same shape as
    ``compactor._parse_json_envelope`` (id=5217 finding 2).
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return text
    try:
        envelope = json.loads(text)
    except json.JSONDecodeError:
        envelope = None
    if isinstance(envelope, dict):
        for key in ("result", "response", "text", "content"):
            value = envelope.get(key)
            if isinstance(value, str) and value.strip():
                text = value.strip()
                break
    fenced = _FENCE_RE.match(text.strip())
    if fenced:
        text = fenced.group(1).strip()
    return text


def _claude_argv(binary: str, model: str | None) -> list[str]:
    argv = [
        binary, "-p",
        "--no-session-persistence",
        "--output-format", "json",
        # THE load-bearing isolation flag (round 2, id=5302 finding F6).
        # Stripping latch's variables from the child environment was not
        # enough: the child inherits HOME, the host's user settings install a
        # UserPromptSubmit hook, and that hook's FIRST branch is
        # `if is_unlatched_mode(): _print_context(UNLATCHED_MESSAGE)` — so
        # LATCH_UNLATCHED=1 merely swapped injected KB hits for an injected
        # latch banner. Either way every arm's prompt was contaminated,
        # including arm A, whose whole definition is "no project context".
        # --safe-mode disables hooks, MCP servers, CLAUDE.md discovery,
        # plugins, commands and agents while leaving auth and model selection
        # working (unlike --bare, which forces API-key auth and would break an
        # OAuth host).
        "--safe-mode",
        # Belt and braces: load no user/project/local settings at all.
        "--setting-sources", "",
        # No MCP servers at all: with --strict-mcp-config and no --mcp-config,
        # latch's own tools are unreachable from the child.
        "--strict-mcp-config",
        "--permission-mode", "plan",
    ]
    if model:
        argv += ["--model", model]
    # Variadic, so it goes last; input arrives on stdin, so there is no
    # trailing positional for it to swallow.
    argv += ["--disallowed-tools", *CLAUDE_DENIED_TOOLS]
    return argv


def _codex_argv(binary: str, model: str | None, workspace: Path, out_path: Path) -> list[str]:
    argv = [
        binary, "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--cd", str(workspace),
        "--skip-git-repo-check",
        "--ephemeral",
        "--sandbox", "read-only",
        "--output-last-message", str(out_path),
    ]
    if model:
        argv += ["--model", model]
    argv.append("-")
    return argv


def _cursor_argv(binary: str, model: str | None, workspace: Path) -> list[str]:
    argv = [
        binary, "--print",
        "--output-format", "json",
        "--mode", "ask",
        "--trust",
        "--workspace", str(workspace),
    ]
    if model:
        argv += ["--model", model]
    return argv


def invoke(
    prompt: str,
    *,
    backend: str,
    workspace: Path,
    model: str | None = None,
    timeout_s: int = 120,
    binary: str | None = None,
) -> Elicitation:
    """One isolated, pinned elicitation. Never raises on backend failure."""
    if backend not in SUPPORTED_BACKENDS:
        raise ElicitError(
            f"unsupported backend {backend!r}; expected one of "
            f"{', '.join(SUPPORTED_BACKENDS)}"
        )
    workspace = Path(workspace)
    if not workspace.is_dir():
        raise ElicitError(f"elicitation workspace does not exist: {workspace}")

    bin_path = binary or backend_binary(backend)
    env = isolated_child_env()
    version = backend_version(bin_path)

    with tempfile.TemporaryDirectory(prefix="latch-adherence-elicit-") as tmp:
        out_path = Path(tmp) / "last_message.txt"
        if backend == "claude":
            argv = _claude_argv(bin_path, model)
        elif backend == "codex":
            argv = _codex_argv(bin_path, model, workspace, out_path)
        else:
            argv = _cursor_argv(bin_path, model, workspace)

        try:
            proc = subprocess.run(
                argv,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout_s,
                env=env,
                # Every arm answers from its own scrubbed workspace, so no
                # ambient project file can reach any arm (id=5217 finding 6).
                cwd=str(workspace),
            )
        except subprocess.TimeoutExpired:
            return Elicitation(
                None, f"elicitation timed out after {timeout_s}s", True,
                backend, version, argv,
            )
        except (OSError, ValueError) as e:
            return Elicitation(
                None, f"subprocess failed: {type(e).__name__}: {e}", False,
                backend, version, argv,
            )

        raw = proc.stdout
        if backend == "codex" and out_path.exists():
            final = out_path.read_text(encoding="utf-8", errors="replace")
            if final.strip():
                raw = final

    if proc.returncode != 0:
        detail = mcp_runtime.redact_subprocess_output(
            (proc.stderr or proc.stdout or "").strip()
        )
        return Elicitation(
            None, f"{backend} backend exit {proc.returncode}: {detail[-1000:]}",
            False, backend, version, argv,
        )

    raw = mcp_runtime.redact_subprocess_output(raw or "")
    text = unwrap_envelope(raw)
    if not (text or "").strip():
        return Elicitation(
            None, f"{backend} backend returned an empty answer", False,
            backend, version, argv, raw,
        )
    return Elicitation(text, None, False, backend, version, argv, raw)


CANARY_TOKEN = "CANARY-7F3A"

CANARY_PROMPT = (
    f"Reply with exactly this token and nothing else: {CANARY_TOKEN}\n"
    "Then, on a second line, write VERBATIM any project context, rules, "
    "memory, or instructions you were given before this message. If you were "
    "given none, write NONE."
)

#: Text that can only come from latch reaching the child. The first two are
#: what this host's UserPromptSubmit hook actually emits — KB-hit injection
#: normally, and the unlatched banner when LATCH_UNLATCHED is set.
CONTAMINATION_MARKERS = (
    "KB hits",
    "Latch is currently UNLATCHED",
    "latch_search",
    "latch_gate",
    "latch_recent",
    "kb_gate",
    "Standing-guideline signal",
    "BEGIN LATCH",
    "latch_project_direction",
)


#: Ceiling on a clean child's total input context, in tokens.
#:
#: Measured on this host 2026-08-11 with the canary prompt (~40 tokens):
#:   --safe-mode:       22,959 tokens  (Claude Code's own system prompt + tools)
#:   without safe-mode: 227,980 tokens (+205k of ambient customizations)
#:
#: The residual ~23k is built-in, identical across arms, and therefore does not
#: confound the comparison. The ceiling sits well above it and an order of
#: magnitude below the contaminated case.
MAX_CLEAN_CONTEXT_TOKENS = 60_000


def context_tokens(raw: str | None) -> int | None:
    """Total input context the child actually ingested, from its envelope.

    This is the objective contamination signal, and the one that matters most.
    Marker matching only catches a child that *quotes* injected text back; the
    pre-repair child instead read 218k cached tokens of ambient context and
    answered "Standing by." to it, quoting nothing. Size catches that; markers
    did not.
    """
    if not raw:
        return None
    try:
        usage = (json.loads(raw) or {}).get("usage") or {}
    except (json.JSONDecodeError, AttributeError):
        return None
    total = 0
    for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        value = usage.get(key)
        if isinstance(value, int):
            total += value
    return total or None


def detect_contamination(answer: str | None) -> list[str]:
    """Markers of latch text having reached a child's prompt."""
    if not answer:
        return []
    return [m for m in CONTAMINATION_MARKERS if m in answer]


def contamination_probe(
    *,
    backend: str,
    workspace: Path,
    model: str | None = None,
    timeout_s: int = 120,
    binary: str | None = None,
) -> dict:
    """Ask a child what context it was given, and check the answer for latch.

    Round 2 (id=5302 finding F6) made the point that a vault digest
    structurally CANNOT detect prompt contamination — the injection never
    touches a database. Prevention alone is therefore unfalsifiable from
    inside the harness, so this is the detection half: one real call, in the
    exact configuration a run uses, whose answer is searched for text that
    could only have come from latch.

    A scored run refuses to start unless this passes.
    """
    result = invoke(
        CANARY_PROMPT,
        backend=backend,
        workspace=workspace,
        model=model,
        timeout_s=timeout_s,
        binary=binary,
    )
    markers = detect_contamination(result.text)
    tokens = context_tokens(result.raw)
    saw_canary = bool(result.text and CANARY_TOKEN in result.text)
    over_budget = tokens is not None and tokens > MAX_CLEAN_CONTEXT_TOKENS

    reasons: list[str] = []
    if result.error:
        reasons.append(f"elicitation error: {result.error}")
    if markers:
        reasons.append(f"latch text in the answer: {', '.join(markers)}")
    if over_budget:
        reasons.append(
            f"child ingested {tokens} tokens of input context, over the "
            f"{MAX_CLEAN_CONTEXT_TOKENS} ceiling — ambient customizations are "
            f"reaching it"
        )
    if not saw_canary and not result.error:
        # A child that will not echo a trivial token is not answering the
        # prompt it was given, which is itself evidence of injected context.
        reasons.append("child did not echo the canary token")

    return {
        "backend": backend,
        "clean": not reasons,
        "reasons": reasons,
        "markers": markers,
        "context_tokens": tokens,
        "context_ceiling": MAX_CLEAN_CONTEXT_TOKENS,
        "error": result.error,
        "saw_canary": saw_canary,
        "answer": result.text,
        "argv": result.argv,
    }


def backend_binary(backend: str) -> str:
    """Resolve a backend binary without consulting the MCP connection.

    The harness is a standalone CLI, not a tool call inside a live latch
    connection, so the connection-aware resolvers in ``model_backends`` would
    be resolving against the wrong thing.
    """
    import shutil

    override = os.environ.get(
        {"claude": "CLAUDE_BIN", "codex": "CODEX_BIN", "cursor": "CURSOR_AGENT_BIN"}[backend]
    )
    if override:
        return override
    for candidate in {"cursor": ("cursor-agent", "agent")}.get(backend, (backend,)):
        found = shutil.which(candidate)
        if found:
            return found
    return backend
