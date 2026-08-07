"""memcore CLI — init, remember, recall, stats, assemble, rules, serve, import.

Layer governance: remember/update accept --layer (P0-P6); writes are ACL-checked
against LAYER_WRITERS with the principal from --who (default system).
"""
from __future__ import annotations

import argparse
import json
import sys

from .embed import load_embedder
from .store import DEFAULT_DB, KNOWLEDGE_TYPES, Store


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="memcore", description="Production-grade agent memory layer")
    p.add_argument("--db", default=None, help=f"SQLite path (default: {DEFAULT_DB})")
    p.add_argument(
        "--embed",
        default=None,
        choices=["ollama", "hash", "none"],
        help="embedder override (default: MEMCORE_EMBED or ollama)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create/migrate the store")

    r = sub.add_parser("remember", help="store a fact/decision/pattern")
    r.add_argument("content")
    r.add_argument("--type", choices=KNOWLEDGE_TYPES, default="fact")
    r.add_argument("--source", default=None)
    r.add_argument("--session", default=None)
    r.add_argument("--layer", choices=["P0", "P1", "P2", "P3", "P4", "P5", "P6"], default="P5")
    r.add_argument("--who", default="system")
    r.add_argument("--confidence", type=float, default=1.0)
    r.add_argument("--ttl-days", type=int, default=None)

    q = sub.add_parser("recall", help="hybrid search (FTS5 + semantic)")
    q.add_argument("query")
    q.add_argument("--limit", type=int, default=10)
    q.add_argument("--no-semantic", action="store_true")
    q.add_argument("--serendipity", action="store_true")
    q.add_argument("--json", action="store_true")

    sub.add_parser("stats", help="store statistics")

    a = sub.add_parser("assemble", help="budget-bounded context assembly")
    a.add_argument("--budget", type=int, default=12000)

    f = sub.add_parser("forget", help="supersede an entry (soft-delete)")
    f.add_argument("id", type=int)
    f.add_argument("--reason", default=None)

    u = sub.add_parser("update", help="supersede + reinsert an entry")
    u.add_argument("id", type=int)
    u.add_argument("--content", default=None)
    u.add_argument("--type", choices=KNOWLEDGE_TYPES, default=None)
    u.add_argument("--confidence", type=float, default=None)
    u.add_argument("--layer", choices=["P0", "P1", "P2", "P3", "P4", "P5", "P6"], default=None)
    ev = sub.add_parser("eval", help="run the eval suite: recall@k (memcore vs drewgent baseline) + ACL compliance")
    ev.add_argument("suite", help="path to eval suite JSON")
    ev.add_argument("--json", action="store_true")

    cw = sub.add_parser("compile-wiki", help="compile active memory into the P5 wiki (markdown)")
    cw.add_argument("dir", help="output directory")

    au = sub.add_parser("audit", help="show recent governance audit entries")
    au.add_argument("--limit", type=int, default=20)
    au.add_argument("--denied-only", action="store_true")

    ct = sub.add_parser("contradictions", help="scan for governance contradictions (rules, cross-layer duplicates)")

    sn = sub.add_parser("snapshot", help="snapshot create/rollback")
    sn_sub = sn.add_subparsers(dest="snapshot_cmd", required=True)
    sc = sn_sub.add_parser("create")
    sc.add_argument("version_id")
    sc.add_argument("--label", default=None)
    sr = sn_sub.add_parser("rollback")
    sr.add_argument("version_id")

    ra = sub.add_parser("rules", help="manage canonical rules")
    ra_sub = ra.add_subparsers(dest="rules_cmd", required=True)
    add = ra_sub.add_parser("add")
    add.add_argument("text")
    add.add_argument("--priority", type=int, default=100)
    add.add_argument("--layer", default="P0")
    add.add_argument("--scope", default=None)
    add.add_argument("--source", default=None)

    sub.add_parser("serve", help="run the MCP stdio server")

    imp = sub.add_parser("import-drewgent", help="import a drewgent knowledge.db into memcore")
    imp.add_argument("src", help="path to drewgent knowledge.db")
    imp.add_argument("--no-embed", action="store_true")

    return p


def _make_store(args) -> Store:
    embedder = load_embedder(args.embed) if args.embed else None
    return Store(args.db, embedder=embedder)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = _make_store(args)

    if args.cmd == "init":
        stats = store.stats()
        print(f"initialized {store.path} (schema v{stats['schema_version']})")
        print(json.dumps(stats, ensure_ascii=False))
        return 0

    if args.cmd == "remember":
        kid = store.add_knowledge(
            args.content,
            type=args.type,
            source=args.source,
            session_id=args.session,
            layer=args.layer,
            who=args.who,
            confidence=args.confidence,
            ttl_days=args.ttl_days,
        )
        print(f"✓ saved #{kid} ({args.type}, {args.layer})")
        if store.last_embed_warning:
            print(f"⚠ embedding skipped: {store.last_embed_warning}", file=sys.stderr)
        return 0

    if args.cmd == "recall":
        results = store.recall(
            args.query,
            limit=args.limit,
            use_semantic=not args.no_semantic,
            serendipity=args.serendipity,
        )
        if args.json:
            print(json.dumps(results, ensure_ascii=False, indent=2))
            return 0
        if not results:
            print("nothing found in memory")
            return 0
        for r in results:
            preview = r["content"][:200].replace("\n", " ").strip()
            sem = f" sem={r['semantic_score']:.3f}" if r["semantic_score"] is not None else ""
            ser = " (serendipity)" if r.get("_serendipity") else ""
            print(f"  [{r['score']:.3f}]{sem} ({r['type']}, {r['layer']}) {preview}{ser}")
        print(f"\n{len(results)} result(s)")
        return 0

    if args.cmd == "stats":
        print(json.dumps(store.stats(), ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "assemble":
        print(store.assemble(budget_chars=args.budget))
        return 0

    if args.cmd == "eval":
        from .eval import format_report, load_suite, run_eval

        suite = load_suite(args.suite)
        result = run_eval(store, suite)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(format_report(result))
        return 0

    if args.cmd == "compile-wiki":
        print(json.dumps(store.compile_wiki(args.dir), ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "audit":
        for e in store.audit_log(limit=args.limit, denied_only=args.denied_only):
            mark = "DENIED " if e["denied"] else "       "
            print(f"{mark}{e['action']:<18} #{e['knowledge_id'] or '-'} {e['layer'] or '-'} {e['who'] or '-'} :: {e['detail'] or ''}")
        return 0

    if args.cmd == "contradictions":
        found = store.contradictions()
        if not found:
            print("no contradictions found")
            return 0
        for c in found:
            print(f"[{c['severity']}] {c['kind']}")
            if c["kind"] == "conflicting_rules":
                print(f"  rule #{c['a']['id']} (pri {c['a']['priority']}): {c['a']['text']}")
                print(f"  rule #{c['b']['id']} (pri {c['b']['priority']}): {c['b']['text']}")
            else:
                print(f"  #{c['a']['id']} [{c['a']['layer']}]: {c['a']['content']}")
                print(f"  #{c['b']['id']} [{c['b']['layer']}]: {c['b']['content']}")
        return 0

    if args.cmd == "forget":
        ok = store.forget(args.id, reason=args.reason)
        print(f"✓ superseded #{args.id}" if ok else f"entry #{args.id} not found or already superseded")
        return 0

    if args.cmd == "update":
        new_id = store.update_knowledge(
            args.id,
            content=args.content,
            type=args.type,
            confidence=args.confidence,
            layer=args.layer,
        )
        print(f"✓ updated #{args.id} → #{new_id}")
        return 0

    if args.cmd == "snapshot":
        if args.snapshot_cmd == "create":
            sid = store.snapshot_create(args.version_id, label=args.label)
            print(f"✓ snapshot '{args.version_id}' created (#{sid})")
        else:
            n = store.snapshot_rollback(args.version_id)
            print(f"✓ rolled back to '{args.version_id}': {n} entry(ies) superseded")
        return 0

    if args.cmd == "rules":
        rid = store.add_rule(
            args.text,
            priority=args.priority,
            layer=args.layer,
            scope=args.scope,
            source=args.source,
        )
        print(f"✓ rule #{rid} [{args.layer}] priority={args.priority}")
        return 0

    if args.cmd == "serve":
        from .mcp import serve

        return serve(store)

    if args.cmd == "import-drewgent":
        from .import_drewgent import import_drewgent

        summary = import_drewgent(args.src, store, reembed=not args.no_embed)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print(f"→ {store.path}")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
