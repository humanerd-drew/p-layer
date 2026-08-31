"""Read-only health and status checks for the p-layer store."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .embed import load_embedder
from .migrations import MIGRATIONS

REQUIRED_TABLES = (
    "schema_migrations",
    "knowledge",
    "knowledge_fts",
    "embeddings",
    "episodes",
    "entities",
    "relations",
    "rules",
    "snapshots",
    "audit_log",
)


def _check(name: str, status: str, detail: str, hint: str | None = None) -> dict:
    result = {"name": name, "status": status, "detail": detail}
    if hint:
        result["hint"] = hint
    return result


def inspect_store(path: str | Path, embed_name: str | None = None) -> dict:
    """Return a health report without creating or migrating the database."""
    db_path = Path(path).expanduser()
    checks: list[dict] = []
    report: dict = {"path": str(db_path), "checks": checks}

    if not db_path.exists():
        checks.append(_check("database", "fail", "database does not exist", "run: p-layer init"))
        report["status"] = "not_initialized"
        return report
    if not db_path.is_file():
        checks.append(_check("database", "fail", "path is not a regular file"))
        report["status"] = "error"
        return report

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        checks.append(_check("database", "fail", f"cannot open read-only: {exc}"))
        report["status"] = "error"
        return report

    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
        missing = [name for name in REQUIRED_TABLES if name not in tables]
        if missing:
            checks.append(_check(
                "schema", "fail", f"missing tables: {', '.join(missing)}", "run: p-layer init"
            ))
        else:
            checks.append(_check("schema", "ok", "required tables present"))

        applied = {}
        if "schema_migrations" in tables:
            applied = {
                int(row[0]): row[1]
                for row in conn.execute(
                    "SELECT version, name FROM schema_migrations ORDER BY version"
                )
            }
        expected = {version: name for version, name, _ in MIGRATIONS}
        unapplied = sorted(set(expected) - set(applied))
        unknown = sorted(set(applied) - set(expected))
        if unapplied or unknown:
            detail = []
            if unapplied:
                detail.append(f"pending={unapplied}")
            if unknown:
                detail.append(f"unknown={unknown}")
            checks.append(_check("migrations", "warn", "; ".join(detail), "run: p-layer init"))
        else:
            checks.append(_check("migrations", "ok", f"schema v{max(applied, default=0)}"))
        report["schema_version"] = max(applied, default=0)

        quick = conn.execute("PRAGMA quick_check").fetchone()[0]
        checks.append(_check(
            "integrity", "ok" if quick == "ok" else "fail", f"PRAGMA quick_check={quick}"
        ))

        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        checks.append(_check(
            "journal",
            "ok" if str(journal).lower() == "wal" else "warn",
            f"journal_mode={journal}",
            "WAL is recommended for concurrent MCP access"
            if str(journal).lower() != "wal" else None,
        ))

        counts = {}
        for table in (
            "knowledge", "embeddings", "episodes", "entities", "relations",
            "rules", "snapshots", "audit_log",
        ):
            if table in tables:
                counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        report["counts"] = counts

        if "knowledge" in tables and "knowledge_fts" in tables:
            knowledge_count = counts.get("knowledge", 0)
            fts_count = int(conn.execute("SELECT COUNT(*) FROM knowledge_fts").fetchone()[0])
            checks.append(_check(
                "fts",
                "ok" if knowledge_count == fts_count else "warn",
                f"knowledge={knowledge_count}, index={fts_count}",
                "run: p-layer init (migration/rebuild may be required)"
                if knowledge_count != fts_count else None,
            ))

        try:
            embedder = load_embedder(embed_name)
            if embedder.semantic:
                detail = f"{embedder.name} configured (availability probe deferred)"
            else:
                detail = f"{embedder.name} configured (keyword fallback; semantic fusion disabled)"
            checks.append(_check("embedding", "ok", detail))
            report["embedder"] = embedder.name
        except (ValueError, OSError) as exc:
            checks.append(_check(
                "embedding", "warn", f"embedding unavailable: {exc}",
                "set P_LAYER_EMBED=hash for offline mode",
            ))
            report["embedder"] = None
    except sqlite3.Error as exc:
        checks.append(_check("database", "fail", f"read failed: {exc}"))
    finally:
        conn.close()

    statuses = {check["status"] for check in checks}
    report["status"] = "error" if "fail" in statuses else "degraded" if "warn" in statuses else "ok"
    return report


def format_report(report: dict) -> str:
    """Format a compact human-readable status/doctor report."""
    lines = [f"p-layer: {report['status']}", f"database: {report['path']}"]
    if "schema_version" in report:
        lines.append(f"schema: v{report['schema_version']}")
    if report.get("embedder"):
        lines.append(f"embedding: {report['embedder']}")
    for check in report.get("checks", []):
        mark = {"ok": "✓", "warn": "⚠", "fail": "✗"}.get(check["status"], "?")
        lines.append(f"{mark} {check['name']}: {check['detail']}")
        if check.get("hint"):
            lines.append(f"  fix: {check['hint']}")
    counts = report.get("counts") or {}
    if counts:
        lines.append("counts: " + ", ".join(f"{key}={value}" for key, value in counts.items()))
    return "\n".join(lines)
