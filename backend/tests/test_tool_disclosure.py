"""Progressive tool disclosure: compact core set, catalog, find/load."""
from app.tools.builtin import load_builtin_tools
from app.tools.disclosure import CORE_TOOLS, LOAD_ALL_BELOW, catalog_text, find_tools, initial_tools, load_tools
from app.tools.base import registry

load_builtin_tools()


def _granted(groups):
    return {t.name: t for g in groups if registry.group(g) for t in registry.group(g).tools}


def test_core_names_exist():
    assert all(registry.get(n) for names in CORE_TOOLS.values() for n in names)


def test_initial_subset_and_small_toolboxes():
    big = _granted(["search", "browser", "rag", "documents", "sheets", "filesystem", "memory", "agents"])
    active = initial_tools(big)
    assert {"search_web", "rag_search", "fs_read", "agent_delegate"} <= active
    assert len(active) < len(big) / 2
    small = _granted(["search", "memory"])
    assert len(small) <= LOAD_ALL_BELOW and initial_tools(small) == set(small)


def test_find_and_load():
    granted = _granted(["sheets", "pdf", "filesystem", "search", "documents", "slides"])
    active = initial_tools(granted)
    hits = find_tools(granted, active, "pivot table")
    assert hits and hits[0]["name"] == "sheet_pivot"
    loaded, unknown = load_tools(granted, active, ["sheet_pivot", "pdf.merge", "email_send"])
    assert loaded == ["sheet_pivot", "pdf_merge"] and unknown == ["email_send"]
    assert "sheet_pivot" in active and "Loaded:" in catalog_text(granted, active)
