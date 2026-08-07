#!/usr/bin/env bash
# p_layer proof pipeline — the full drewgent -> p_layer migration story.
#
#   1. builds a drewgent-style workspace fixture (knowledge.db WITH sessions,
#      entities, relations, plus vault files: rules.md and P6 incidents)
#   2. imports it (import-drewgent now carries sessions too)
#   3. ingests the vault: import-rules, import-incidents
#   4. evals recall@k BEFORE governance vs AFTER (confidence metadata)
#   5. shows a denied write landing in the audit log
#   6. runs graph explore/trace/rca + transitive closure
#   7. scans contradictions and compiles the P5 wiki
#
# Run:  bash examples/demo_import_eval.sh
set -euo pipefail

WORK="$(mktemp -d)"
SRC="$WORK/drewgent.db"
DST="$WORK/memory.db"
SUITE="$WORK/suite.json"
WIKI="$WORK/wiki"
RULES="$WORK/vault/P0-brainstem/rules.md"
INCIDENTS="$WORK/vault/P6-prefrontal/incidents"

python3 - "$SRC" <<'PY'
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.executescript("""
CREATE TABLE knowledge (id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL DEFAULT 'fact', content TEXT NOT NULL,
    source TEXT, created_at TEXT);
CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, created_at TEXT, message_count INTEGER DEFAULT 0);
CREATE TABLE entities (id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL, type TEXT NOT NULL, type_parent TEXT, properties TEXT DEFAULT '{}');
CREATE TABLE relations (id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL, target_id INTEGER NOT NULL,
    type TEXT NOT NULL, properties TEXT DEFAULT '{}');
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
db.execute("INSERT INTO sessions (id, title, message_count) VALUES ('s1','2026-08-07 payments', 12)")
db.execute("INSERT INTO entities (label, type) VALUES ('portone','tool')")
db.execute("INSERT INTO entities (label, type) VALUES ('deploy-failed','incident')")
db.execute("INSERT INTO entities (label, type) VALUES ('retry-policy','pattern')")
db.execute("INSERT INTO relations (source_id, target_id, type) VALUES (2,1,'references')")
db.execute("INSERT INTO relations (source_id, target_id, type) VALUES (2,3,'fixed_by')")
db.execute("INSERT INTO relations (source_id, target_id, type) VALUES (3,1,'depends_on')")
db.commit()
db.close()
PY

mkdir -p "$(dirname "$RULES")" "$INCIDENTS"
cat > "$RULES" <<'MD'
# Brain Rules
## [P0] never expose secrets in logs
priority: 10
scope: all
## [P1] be concise in responses
MD
cat > "$INCIDENTS/2026-08-01-deploy.md" <<'MD'
# deploy failed
root cause: payment gateway retry raced the webhook deadline
MD

cp examples/suite.example.json "$SUITE"

export P_LAYER_EMBED=hash
export P_LAYER_DB="$DST"

echo "== 1. import drewgent workspace (knowledge + sessions + ontology) =="
python3 -m p_layer import-drewgent "$SRC" --no-embed

echo
echo "== 2. ingest the vault (rules.md -> rules, incidents -> episodes) =="
python3 -m p_layer import-rules "$RULES"
python3 -m p_layer import-incidents "$INCIDENTS"

echo
echo "== 3. BEFORE governance: imported flat, all confidence 1.0 =="
python3 -m p_layer eval "$SUITE"

echo
echo "== decoy (#2) marked low-confidence, truth (#3) stays 1.0 =="
python3 -m p_layer update 2 --confidence 0.2 >/dev/null

echo
echo "== AFTER governance: confidence-ranked =="
python3 -m p_layer eval "$SUITE"

echo
echo "== 4. denied write is on the record =="
python3 -m p_layer remember "rotate the api key weekly" --layer P0 --who agent 2>/dev/null || echo "(denied, exit=$?)"
python3 -m p_layer audit --denied-only

echo
echo "== 5. graph: explore / trace / rca / closure =="
python3 -m p_layer graph explore "portone"
python3 -m p_layer graph rca "deploy-failed"
python3 -m p_layer graph closure 3 --rel depends_on

echo
echo "== 6. contradictions =="
python3 -m p_layer rules add "never expose secrets in logs" --priority 100 >/dev/null
python3 -m p_layer contradictions || true

echo
echo "== 7. P5 wiki =="
python3 -m p_layer compile-wiki "$WIKI" >/dev/null
find "$WIKI" -type f -name "*.md" | sort
