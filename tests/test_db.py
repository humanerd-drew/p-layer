"""Tests for KnowledgeDB (SQLite mode — no PG dependency)."""

import json
import os
import tempfile

import pytest

from p_layer.core.db import (
    KnowledgeDB,
    LAYER_AUTHORITY,
    LAYER_WRITERS,
    WriteDenied,
)


@pytest.fixture
def db():
    tmp = tempfile.mkdtemp()
    old_dir = os.environ.get("KNOWLEDGE_DB_DIR")
    os.environ["KNOWLEDGE_DB_DIR"] = tmp
    instance = KnowledgeDB(mode="sqlite")
    yield instance
    instance.close()
    if old_dir is not None:
        os.environ["KNOWLEDGE_DB_DIR"] = old_dir
    else:
        os.environ.pop("KNOWLEDGE_DB_DIR", None)


class TestInsert:
    def test_insert_and_search(self, db):
        result = db.insert(
            layer="P5", type="fact",
            content="P-layer organizes agent memory into 7 governance layers.",
            who="system:test",
        )
        assert result["id"] is not None
        assert result["layer"] == "P5"
        assert result["type"] == "fact"

        results = db.search("governance", limit=10)
        assert len(results) >= 1
        assert "governance" in results[0]["content"]

    def test_insert_no_results(self, db):
        results = db.search("xyznonexistent", limit=10)
        assert results == []

    def test_insert_invalid_layer(self, db):
        with pytest.raises(ValueError, match="Invalid layer"):
            db.insert(layer="P99", type="fact", content="bad")

    def test_insert_write_denied(self, db):
        with pytest.raises(WriteDenied, match="does not allow"):
            db.insert(layer="P0", type="fact", content="secret",
                      who="unauthorized:user")

    def test_insert_stores_metadata(self, db):
        result = db.insert(
            layer="P4", type="pattern",
            content="test metadata storage",
            who="system:agent", source="test_script",
        )
        assert result["who"] == "system:agent"
        assert result["source"] == "test_script"
        assert result["layer"] == "P4"

    def test_insert_empty_content(self, db):
        result = db.insert(layer="P6", type="incident", content="")
        assert result["id"] is not None


class TestLayerPermissions:
    def test_layer_authority_values(self):
        assert LAYER_AUTHORITY["P0"] == 100
        assert LAYER_AUTHORITY["P6"] == 20
        assert LAYER_AUTHORITY["P3"] == 50

    def test_layer_writers_p0_system_only(self):
        assert LAYER_WRITERS["P0"] == frozenset({"system"})
        assert "agent" not in LAYER_WRITERS["P0"]

    def test_layer_writers_p6_wider_access(self):
        assert "manual" in LAYER_WRITERS["P6"]
        assert "tool" in LAYER_WRITERS["P5"]


class TestSQLiteFallback:
    def test_sqlite_mode_available(self, db):
        assert db.available is True
        assert db.mode == "sqlite"

    def test_sqlite_fts_search(self, db):
        db.insert(layer="P5", type="fact",
                  content="vector search is for embeddings")
        results = db.search("embeddings", limit=5)
        assert len(results) >= 1

    def test_sqlite_fts_no_match(self, db):
        results = db.search("zzzzzzzzzzz", limit=5)
        assert results == []


class TestEdgeCases:
    def test_empty_search_string(self, db):
        db.insert(layer="P5", type="fact", content="anything")
        results = db.search("", limit=5)
        assert len(results) >= 1

    def test_has_pg_property(self, db):
        assert db.has_pg is False

    def test_available_property(self, db):
        assert db.available is True

    def test_get_layer_count_sqlite(self, db):
        counts = db.get_layer_count()
        assert counts == {"sqlite": True}

    def test_graph_query_sqlite_empty(self, db):
        result = db.graph_query("nonexistent")
        assert result == {"nodes": [], "edges": []}

    def test_graph_query_sqlite_with_entity(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KNOWLEDGE_DB_DIR", str(tmp_path))
        kdb = KnowledgeDB(mode="sqlite")
        kdb._sqlite_conn.execute("INSERT INTO entities (label, entity_type) VALUES (?, ?)", ("test-entity", "concept"))
        kdb._sqlite_conn.execute("INSERT INTO entities (label, entity_type) VALUES (?, ?)", ("related-entity", "tool"))
        kdb._sqlite_conn.execute("INSERT INTO relations (source_id, target_id, rel_type) VALUES (?, ?, ?)",
                                 (1, 2, "depends_on"))
        kdb._sqlite_conn.commit()
        result = kdb.graph_query("test-entity", depth=2)
        assert len(result["nodes"]) == 2
        assert len(result["edges"]) == 1
        assert result["edges"][0]["type"] == "depends_on"
        kdb.close()

    def test_vector_search_sqlite(self, db):
        result = db.vector_search([0.1] * 10)
        assert result == []

    def test_hybrid_search_sqlite_fallback(self, db):
        db.insert(layer="P5", type="fact",
                  content="hybrid fallback test")
        results = db.hybrid_search("hybrid", [0.1] * 10)
        assert len(results) >= 1

    def test_sql_injection_layer_name(self, db):
        with pytest.raises(ValueError, match="Invalid layer"):
            db.insert(
                layer="P5' OR '1'='1",
                type="fact",
                content="injection attempt",
            )

    def test_truncation_long_content(self, db):
        long = "x" * 200_000
        result = db.insert(layer="P6", type="fact", content=long)
        assert result["id"] is not None

    def test_context_manager(self):
        tmp = tempfile.mkdtemp()
        os.environ["KNOWLEDGE_DB_DIR"] = tmp
        with KnowledgeDB(mode="sqlite") as kdb:
            r = kdb.insert(layer="P5", type="fact", content="cm test")
            assert r["id"] is not None
        os.environ.pop("KNOWLEDGE_DB_DIR", None)


class TestSetMode:
    def test_set_mode_invalid(self, db):
        with pytest.raises(ValueError, match="Invalid mode"):
            db.set_mode("invalid")
