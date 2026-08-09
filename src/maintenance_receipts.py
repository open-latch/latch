"""Durable foreground receipts for autonomous maintenance blockers."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import paths


STATE_FILENAME = "maintenance_receipts.json"
STATE_VERSION = 1
MAX_ITEMS = 20


def _state_path(project_path: str | None) -> Path:
    return paths.project_dir(project_path) / STATE_FILENAME


def _load(project_path: str | None) -> dict:
    path = _state_path(project_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": STATE_VERSION, "items": []}
    except (OSError, ValueError):
        return {"version": STATE_VERSION, "items": []}
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        return {"version": STATE_VERSION, "items": []}
    return payload


def _save(project_path: str | None, state: dict) -> None:
    path = _state_path(project_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _receipt_text(result: dict) -> tuple[str, str, str, str]:
    failure = str(result.get("failure_kind") or result.get("reason") or "failed")
    attempts = [
        item for item in (result.get("backend_attempts") or [])
        if isinstance(item, dict)
    ]
    backends = [str(item.get("backend")) for item in attempts if item.get("backend")]
    backend_label = ", ".join(dict.fromkeys(backends)) or "configured"
    if failure == "authentication":
        blocker = f"authentication is unavailable for autonomous {backend_label} maintenance"
        remediation = (
            "authenticate the standalone CLI used by autonomous maintenance, or save an "
            "explicit approved fallback order, then start a supported host session to retry"
        )
    elif failure == "missing_executable":
        blocker = f"the configured autonomous {backend_label} executable is unavailable"
        remediation = (
            "rerun Latch quickstart to refresh the vault runner, or save an explicit "
            "approved fallback order, then start a supported host session to retry"
        )
    elif failure in {"budget_blocked", "attempt_cap"}:
        blocker = "the bounded tree rebuild could not finish within this run's invocation allowance"
        remediation = "leave the retry pending; the next maintenance opportunity resumes staged summaries"
    elif failure == "configuration":
        blocker = "the autonomous maintenance backend policy is invalid or incomplete"
        remediation = "rerun Latch quickstart and explicitly approve any desired fallback order"
    else:
        blocker = f"the autonomous {backend_label} tree rebuild failed before a replacement was ready"
        remediation = "inspect the local maintenance log, repair the configured runner, and retry"
    impact = (
        "the last known-good hierarchy remains active; new or changed nodes are not yet "
        "reflected in rebuilt summaries, while direct search remains available"
    )
    text = f"Latch tree maintenance is blocked: {blocker}. Impact: {impact}. Remediation: {remediation}."
    return blocker, impact, remediation, text


def record_tree_blocker(project_path: str | None, result: dict) -> dict:
    """Persist/dedupe one privacy-safe blocker for the next foreground read."""
    blocker, impact, remediation, text = _receipt_text(result)
    signature_payload = {
        "reason": str(result.get("reason") or "failed"),
        "failure_kind": str(result.get("failure_kind") or ""),
        "backends": [
            str(item.get("backend"))
            for item in (result.get("backend_attempts") or [])
            if isinstance(item, dict) and item.get("backend")
        ],
    }
    signature = hashlib.sha256(
        json.dumps(signature_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()[:20]
    state = _load(project_path)
    for item in state["items"]:
        if item.get("signature") == signature and item.get("surfaced_at") is None:
            return dict(item)
    now = datetime.now(timezone.utc).isoformat()
    receipt_id = hashlib.sha256(f"{signature}:{now}".encode("utf-8")).hexdigest()[:20]
    item = {
        "id": receipt_id,
        "signature": signature,
        "surface_kind": "maintenance_blocker",
        "op": "tree_rebuild",
        "blocker": blocker,
        "impact": impact,
        "remediation": remediation,
        "text": text,
        "created_at": now,
        "surfaced_at": None,
    }
    state["version"] = STATE_VERSION
    state["items"] = [*state["items"], item][-MAX_ITEMS:]
    _save(project_path, state)
    return dict(item)


def pending_blockers(project_path: str | None, *, limit: int = 1) -> list[dict]:
    items = [item for item in _load(project_path)["items"] if item.get("surfaced_at") is None]
    items.sort(key=lambda item: str(item.get("created_at") or ""))
    return [dict(item) for item in items[:max(1, int(limit))]]


def claim_blocker(project_path: str | None, receipt_id: str) -> dict:
    state = _load(project_path)
    for item in state["items"]:
        if str(item.get("id")) != str(receipt_id):
            continue
        if item.get("surfaced_at") is not None:
            return {"created": False, "surface_kind": "maintenance_blocker"}
        item["surfaced_at"] = datetime.now(timezone.utc).isoformat()
        _save(project_path, state)
        return {"created": True, "surface_kind": "maintenance_blocker"}
    return {"created": False, "surface_kind": "maintenance_blocker"}
