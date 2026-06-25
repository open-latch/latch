"""Server-managed rolling "Latest" region for living-summary node bodies.

Slice 2a of the kb_append work (spec id=1519; targets the token round-trip
measured in id=1509). The region lives at the TOP of the body so the
SessionStart brief and kb_get — which surface the body head — show the newest
state with NO read-path change. Entries are newest-first and capped, so a
workstream body stays terse (restores the stable-pointer intent, id=353 /
id=339) instead of accreting the "Recent ship reports" bloat that produced the
~9.5 KB bodies (id=1320).

Marker-delimited with HTML comments so the region is invisible in rendered
markdown yet trivially parseable for the next append. Pure text transform — no
DB, no embedding, no clock; cross-platform (id=1330). The caller supplies the
date stamp so this stays deterministic and testable.
"""
from __future__ import annotations

START = "<!--latch:rolling-->"
END = "<!--/latch:rolling-->"
HEADER = "## Latest (rolling)"
DEFAULT_CAP = 3


def _split(body: str) -> tuple[list[str], str]:
    """Return (entries, rest). `entries` are the newest-first bullet lines inside
    the rolling markers; `rest` is the body with the region removed. When no
    region is present, returns ([], body)."""
    if START not in body or END not in body:
        return [], body
    pre, _, after_start = body.partition(START)
    region, _, post = after_start.partition(END)
    entries = [ln.rstrip() for ln in region.splitlines() if ln.strip().startswith("- ")]
    rest = (pre + post).strip("\n")
    return entries, rest


def apply(body: str, text: str, *, date: str, cap: int = DEFAULT_CAP) -> str:
    """Add `text` as the newest entry in the rolling region at the top of `body`,
    keep at most `cap` entries (newest-first, oldest evicted), and return the new
    body. Creates the region if absent. `date` is a caller-supplied stamp."""
    entry_text = " ".join((text or "").split())  # collapse to a single line
    entries, rest = _split(body)
    entries = [f"- {date}: {entry_text}", *entries][:max(1, cap)]
    region = "\n".join([START, HEADER, *entries, END])
    return f"{region}\n\n{rest}" if rest else f"{region}\n"


def strip_markers(body: str) -> str:
    """Drop the HTML-comment marker lines for clean display, keeping the header
    and entries. The region renders invisibly in markdown either way; this is for
    raw-text consumers that would otherwise show the comment lines."""
    return "\n".join(ln for ln in body.splitlines() if ln.strip() not in (START, END))
