"""p-layer core — KnowledgeDB with PostgreSQL (primary) + SQLite (fallback)."""

from .db import KnowledgeDB as KnowledgeDB
from .memory import recall_ranked as recall_ranked
from .ontology import ONTOLOGY as ONTOLOGY
