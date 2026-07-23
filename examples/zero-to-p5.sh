#!/usr/bin/env bash
# zero-to-p5.sh — Full pipeline from empty project to compiled wiki.
# Run from your project root.

set -euo pipefail

echo "=== 1. Install p-layer ==="
pip install p-layers -q

echo "=== 2. Seed the knowledge database ==="
python3 -m p_layer --help > /dev/null 2>&1 || true  # warm up
python3 scripts/seed_knowledge_db.py

echo "=== 3. Initialize ontology ==="
python3 scripts/ontology_setup.py

echo "=== 4. Ingest a fact ==="
python3 scripts/ingest_fact.py "P-layer is a knowledge governance system for AI agents."

echo "=== 5. Compile wiki ==="
python3 scripts/wiki_compile.py --output wiki/compiled

echo "=== 6. Lint wiki ==="
python3 scripts/wiki_lint.py --wiki-dir wiki/compiled

echo "=== Done. Start MCP server: ==="
echo "  python3 -m p_layer.mcp.server"
