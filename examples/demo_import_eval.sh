#!/usr/bin/env bash
# memcore proof pipeline — same data, two engines, one story.
#
#   1. builds a drewgent-style knowledge.db fixture
#   2. imports it into memcore (import-drewgent)
#   3. evals recall@k BEFORE governance (flat import) vs AFTER (confidence metadata)
#   4. shows a denied write landing in the audit log
#   5. scans contradictions and compiles the P5 wiki
#
# Run:  bash examples/demo_import_eval.sh
set -euo pipefail

WORK="$(mktemp -d)"
SRC="$WORK/drewgent.db"
DST="$WORK/memory.db"
SUITE="$WORK/suite.json"
WIKI="$WORK/wiki"

python3 - "$SRC" <<'PY'
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.executescript("""
CREATE TABLE knowledge (id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL DEFAULT 'fact', content TEXT NOT NULL,
    source TEXT, created_at TEXT);
""")
rows = [
    ("decision",  "switched to portone v2 for payments", "session: 2026-08-07"),
    ("pattern",   "payment gateway retries on failure", None),
    ("pattern",   "payment gateway retries with exponential backoff per provider docs", None),
    ("preference","client prefers weekly sync calls", None),
    ("fact",      "deploy pipeline is green", None),
]
for t, c, s in rows:
    db.execute("INSERT INTO knowledge (type, content, source, created_at) VALUES (?,?,?,datetime('now'))", (t, c, s))
db.commit()
db.close()
PY

cp examples/suite.example.json "$SUITE"

export MEMCORE_EMBED=hash
export MEMCORE_DB="$DST"

echo "== import drewgent fixture =="
python3 -m memcore import-drewgent "$SRC" --no-embed

echo
echo "== BEFORE governance: imported flat, all confidence 1.0 =="
python3 -m memcore eval "$SUITE"

echo
echo "== decoy (#2) marked low-confidence, truth (#3) stays 1.0 =="
python3 -m memcore update 2 --confidence 0.2 >/dev/null

echo
echo "== AFTER governance: confidence-ranked =="
python3 -m memcore eval "$SUITE"

echo
echo "== denied write is on the record =="
python3 -m memcore remember "rotate the api key weekly" --layer P0 --who agent 2>/dev/null || echo "(denied, exit=$?)"
python3 -m memcore audit --denied-only

echo
echo "== contradictions =="
python3 -m memcore rules add "never expose secrets in logs" --priority 10 >/dev/null
python3 -m memcore rules add "never expose secrets" --priority 100 >/dev/null
python3 -m memcore contradictions || true

echo
echo "== P5 wiki =="
python3 -m memcore compile-wiki "$WIKI" >/dev/null
find "$WIKI" -type f -name "*.md" | sort
