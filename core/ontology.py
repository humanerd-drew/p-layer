"""Ontology type hierarchy and relation constraints.

Reusable across both PostgreSQL and SQLite backends.
"""

TYPE_HIERARCHY = {
    "artifact": None,
    "doc": "artifact",
    "code": "artifact",
    "project": "artifact",
    "agent": None,
    "persona": "agent",
    "tool": "agent",
    "script": "agent",
    "skill": "agent",
    "decision": None,
    "pattern": "decision",
    "preference": "decision",
    "event": None,
    "incident": "event",
    "session": "event",
    "knowledge": None,
    "concept": "knowledge",
    "paper": "knowledge",
    "reference": "knowledge",
    "meta": None,
    "category": "meta",
    "_task": "meta",
    "fact": "meta",
}

RELATION_CONSTRAINTS = {
    "depends_on": (None, ("tool", "script", "skill")),
    "fixed_by": (("incident",), ("pattern", "decision")),
    "caused": (("decision", "pattern"), ("incident",)),
    "led_to": (("decision", "pattern", "preference"), ("decision", "pattern", "preference")),
    "implements": (("script",), ("pattern", "decision")),
    "contradicts": (("decision", "pattern", "preference"), ("decision", "pattern", "preference")),
    "cites": (("paper",), ("paper",)),
    "references": None,
    "relates_to": None,
    "subtype_of": None,
    "belongs_to": None,
}

ONTOLOGY = {
    "type_hierarchy": TYPE_HIERARCHY,
    "relation_constraints": RELATION_CONSTRAINTS,
}
