"""P0 ontology review gate.

Protects a P0 ontology JSONL (entries: id/type/space/title/file/links) with a
human approval gate. Nothing edits the ontology except `apply` on an
`approved` proposal. All paths are environment-driven (cwd defaults) so the
module works in any project:

  P0_ONTOLOGY     ontology JSONL path          (default: ./p0-brain-ontology.jsonl)
  P0_PROPOSALS    proposals directory          (default: ./rule_proposals)

Lifecycle: propose(proposed) -> approve(approved, human gate) -> apply(applied)
           -> deprecate(deprecated). Idempotent: re-apply is a no-op; duplicate
           ids are never appended. `validate`/`fresh` are read-only.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

ONTOLOGY = Path(os.environ.get("P0_ONTOLOGY", str(Path.cwd() / "p0-brain-ontology.jsonl")))
PROPOSALS_DIR = Path(os.environ.get("P0_PROPOSALS", str(Path.cwd() / "rule_proposals")))

ALLOWED_TYPES = frozenset({"document", "policy", "neuron"})
STATUSES = ("proposed", "approved", "applied", "deprecated")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_entry(entry: dict) -> list[str]:
    """Return a list of schema violations for an ontology entry ([] = valid)."""
    errs = []
    for f in ("id", "type", "space", "title", "file"):
        v = entry.get(f)
        if not isinstance(v, str) or not v.strip():
            errs.append(f"field {f!r} missing/empty")
    if entry.get("type") not in ALLOWED_TYPES:
        errs.append(f"type {entry.get('type')!r} not allowed: {sorted(ALLOWED_TYPES)}")
    if not isinstance(entry.get("links"), list) or not all(
        isinstance(x, str) for x in entry.get("links", [])
    ):
        errs.append("links must be a list of strings")
    return errs


def validate(ontology_path: Path | None = None) -> dict:
    """Validate the ontology JSONL. Returns {"ok": bool, "errors": [...], "count": int}."""
    path = ontology_path or ONTOLOGY
    if not path.exists():
        return {"ok": False, "errors": [f"ontology file missing: {path}"], "count": 0}
    errs, seen = [], {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError as e:
            errs.append(f"{lineno}: JSON parse error — {e}")
            continue
        if not isinstance(entry, dict):
            errs.append(f"{lineno}: not a JSON object")
            continue
        for e in validate_entry(entry):
            errs.append(f"{lineno}: {e}")
        eid = entry.get("id")
        if isinstance(eid, str) and eid:
            if eid in seen:
                errs.append(f"{lineno}: duplicate id {eid!r} (first at {seen[eid]})")
            else:
                seen[eid] = lineno
    count = sum(1 for l in path.read_text(encoding="utf-8").splitlines() if l.strip())
    return {"ok": not errs, "errors": errs, "count": count}


def _load_proposal(pid: str, proposals_dir: Path | None = None) -> dict | None:
    path = (proposals_dir or PROPOSALS_DIR) / f"{pid}.json"
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def propose(pid: str, entry_type: str, space: str, title: str, file: str,
            source: str, links: str = "", target: str = "",
            proposals_dir: Path | None = None) -> dict:
    """Create a proposal. Returns {"ok", "message"}.

    ``target`` (optional) sets the ontology entry id the proposal acts on —
    used by retire proposals where the proposal id differs from the entry id.
    """
    d = proposals_dir or PROPOSALS_DIR
    if not pid or "/" in pid or "\\" in pid:
        return {"ok": False, "message": "id must be non-empty without path separators"}
    if _load_proposal(pid, d):
        return {"ok": False, "message": f"proposal {pid!r} already exists (no re-propose)"}
    entry = {
        "id": target or pid,
        "type": entry_type,
        "space": space,
        "title": title,
        "file": file,
        "links": [x.strip() for x in links.split(",") if x.strip()],
    }
    errs = validate_entry(entry)
    if errs:
        return {"ok": False, "message": "; ".join(errs)}
    d.mkdir(parents=True, exist_ok=True)
    doc = {
        "schemaVersion": 1,
        "id": pid,
        "entry": entry,
        "source_ref": source,
        "status": "proposed",
        "created_at": _now(),
        "updated_at": _now(),
        "history": [{"at": _now(), "to": "proposed"}],
    }
    (d / f"{pid}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "message": f"proposal {pid} created (proposed, source={source})"}


def _transition(pid: str, to: str, proposals_dir: Path | None = None) -> dict:
    doc = _load_proposal(pid, proposals_dir)
    if doc is None:
        return {"ok": False, "message": f"proposal {pid!r} not found"}
    if to not in STATUSES:
        return {"ok": False, "message": f"status {to!r} not allowed: {STATUSES}"}
    cur = doc.get("status")
    if cur == to:
        return {"ok": True, "message": f"{pid} already {to} (no-op)"}
    if to == "applied" and cur != "approved":
        return {"ok": False, "message": f"{pid} is {cur} — only approved proposals can be applied (human gate)"}
    if to == "deprecated" and cur not in ("applied", "approved"):
        return {"ok": False, "message": f"{pid} is {cur} — only applied/approved can be deprecated"}
    doc["status"] = to
    doc["updated_at"] = _now()
    doc.setdefault("history", []).append({"at": _now(), "to": to})
    ((proposals_dir or PROPOSALS_DIR) / f"{pid}.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"ok": True, "message": f"{pid}: {cur} -> {to}"}


def approve(pid: str, proposals_dir: Path | None = None) -> dict:
    return _transition(pid, "approved", proposals_dir)


def deprecate(pid: str, proposals_dir: Path | None = None) -> dict:
    return _transition(pid, "deprecated", proposals_dir)


def apply(pid: str, ontology_path: Path | None = None, proposals_dir: Path | None = None) -> dict:
    """Append an approved proposal's entry to the ontology. Idempotent."""
    doc = _load_proposal(pid, proposals_dir)
    if doc is None:
        return {"ok": False, "message": f"proposal {pid!r} not found"}
    if doc.get("status") == "applied":
        return {"ok": True, "message": f"{pid} already applied (no-op, no duplicate append)"}
    if doc.get("status") != "approved":
        return {"ok": False, "message": f"{pid} is {doc.get('status')} — only approved can be applied (human gate)"}
    entry = doc.get("entry")
    if not isinstance(entry, dict):
        return {"ok": False, "message": f"{pid} entry malformed"}
    errs = validate_entry(entry)
    if errs:
        return {"ok": False, "message": "; ".join(errs)}
    path = ontology_path or ONTOLOGY
    if path.exists():
        for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not raw.strip():
                continue
            try:
                existing = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(existing, dict) and existing.get("id") == entry["id"]:
                return {"ok": True, "message": f"id {entry['id']!r} already at line {lineno} — duplicate append blocked (no-op)"}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    _transition(pid, "applied", proposals_dir)
    doc = _load_proposal(pid, proposals_dir)  # refresh for return
    return {"ok": True, "message": f"{pid} applied (id {entry['id']})"}


def retire(pid: str, ontology_path: Path | None = None, proposals_dir: Path | None = None) -> dict:
    """Remove an ontology entry through the approval gate (2026-08-13).

    Same governance as ``apply``: only an approved proposal may remove a line.
    The proposal's ``entry.id`` (or ``target``) names the line to remove;
    ``source_ref`` preserves the removal reason. Idempotent — a missing id is
    a no-op, and an already-applied proposal is not re-processed.
    """
    doc = _load_proposal(pid, proposals_dir)
    if doc is None:
        return {"ok": False, "message": f"proposal {pid!r} not found"}
    if doc.get("status") == "applied":
        return {"ok": True, "message": f"{pid} already applied (no-op, no double retire)"}
    if doc.get("status") != "approved":
        return {"ok": False, "message": f"{pid} is {doc.get('status')} — only approved can retire (human gate)"}
    entry = doc.get("entry")
    if not isinstance(entry, dict) or not entry.get("id"):
        return {"ok": False, "message": f"{pid} entry malformed"}
    target = entry["id"]
    path = ontology_path or ONTOLOGY
    if not path.exists():
        return {"ok": False, "message": f"ontology file missing: {path}"}
    lines = path.read_text(encoding="utf-8").splitlines()
    kept, removed = [], 0
    for raw in lines:
        if not raw.strip():
            continue
        try:
            existing = json.loads(raw)
        except json.JSONDecodeError:
            kept.append(raw)
            continue
        if isinstance(existing, dict) and existing.get("id") == target:
            removed += 1
            continue
        kept.append(raw)
    if removed == 0:
        return {"ok": True, "message": f"id {target!r} not in ontology — no-op (already removed?)"}
    path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    _transition(pid, "applied", proposals_dir)
    return {"ok": True, "message": f"{target} removed ({removed} line(s), proposal {pid})"}


def fresh(ontology_path: Path | None = None, proposals_dir: Path | None = None) -> dict:
    """Read-only freshness report: last modified + open proposals + validation."""
    path = ontology_path or ONTOLOGY
    v = validate(path)
    if not path.exists():
        return {"ok": False, "status": "failure", "errors": v["errors"]}
    mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
    open_props = []
    d = proposals_dir or PROPOSALS_DIR
    if d.is_dir():
        for p in sorted(d.glob("*.json")):
            try:
                doc = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(doc, dict) and doc.get("status") in ("proposed", "approved"):
                open_props.append(doc.get("id", p.stem))
    if not v["ok"]:
        return {"ok": False, "status": "failure", "last_modified": mtime,
                "open_proposals": len(open_props), "errors": v["errors"]}
    return {"ok": True, "status": "ok", "last_modified": mtime,
            "open_proposals": len(open_props), "entries": v["count"]}
