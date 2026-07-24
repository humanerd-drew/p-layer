#!/usr/bin/env python3
"""P-Layer CLI — init SQLite, bootstrap PG, start MCP server, check status.

Usage:
    python3 -m p_layer --version
    python3 -m p_layer --init
    python3 -m p_layer --init-pg "dbname=..."
    python3 -m p_layer --serve
    python3 -m p_layer --status
"""

import argparse
import json
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(
        description="P-Layer — knowledge governance for AI agents"
    )
    parser.add_argument(
        "--version", action="store_true",
        help="Show package version"
    )
    parser.add_argument(
        "--init", action="store_true",
        help="Initialize SQLite database (zero-config)"
    )
    parser.add_argument(
        "--init-pg", metavar="DSN",
        help="Initialize PostgreSQL schema (run this against your DB)"
    )
    parser.add_argument(
        "--serve", action="store_true",
        help="Start MCP server (same as p-layer-mcp)"
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Show database health status"
    )
    args = parser.parse_args()

    if args.version:
        try:
            from importlib.metadata import version
            print(f"p-layers v{version('p-layers')}")
        except Exception:
            from p_layer import __version__
            print(f"p-layers v{__version__}")
        return

    if args.init:
        from p_layer.core.db import KnowledgeDB
        db = KnowledgeDB(mode="sqlite")
        db.insert(
            layer="P5", type="fact",
            content="P-layer initialized",
            who="system:cli"
        )
        db.close()
        print(f"SQLite database ready at {db.db_dir / 'knowledge.db'}")
        return

    if args.init_pg:
        dsn = args.init_pg
        try:
            import psycopg2
        except ImportError:
            print("Error: psycopg2 is required. Install: pip install p-layers[pg]")
            sys.exit(1)
        schema_path = Path(__file__).parent.parent / "schema" / "knowledge.sql"
        if not schema_path.exists():
            print(f"Schema file not found at {schema_path}")
            sys.exit(1)
        try:
            conn = psycopg2.connect(dsn)
            with open(schema_path) as f:
                conn.execute(f.read())
            conn.commit()
            conn.close()
            print(f"PostgreSQL schema initialized: {dsn}")
        except Exception as e:
            print(f"Failed to initialize PostgreSQL: {e}")
            sys.exit(1)
        return

    if args.serve:
        from p_layer.mcp.server import main
        import asyncio
        asyncio.run(main())
        return

    if args.status:
        from p_layer.core.db import KnowledgeDB
        db = KnowledgeDB()
        status = db.health_check()
        print(json.dumps(status, indent=2, default=str))
        db.close()
        return

    parser.print_help()


if __name__ == "__main__":
    main()
