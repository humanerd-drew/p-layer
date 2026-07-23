#!/usr/bin/env python3
"""Lint compiled wiki: check for broken wikilinks, orphan pages, INDEX consistency.

Usage:
    python3 scripts/wiki_lint.py [--wiki-dir <path>]
"""

import argparse
import json
import re
from pathlib import Path

DEFAULT_WIKI = Path(__file__).resolve().parent.parent / "wiki" / "compiled"


def lint_wiki(wiki_dir: Path):
    if not wiki_dir.is_dir():
        print(f"Wiki directory not found: {wiki_dir}")
        return

    # Load INDEX
    index_path = wiki_dir / "INDEX.json"
    if not index_path.exists():
        print("!! INDEX.json not found")
        return

    index = json.loads(index_path.read_text(encoding="utf-8"))
    all_pages = {entry["slug"] for entry in index}
    all_labels = {entry["label"] for entry in index}

    issues = []

    for entry in index:
        page_path = wiki_dir / f"{entry['slug']}.md"
        if not page_path.exists():
            issues.append(f"ORPHAN in INDEX: {entry['slug']}.md (INDEX has it, file missing)")
            continue

        text = page_path.read_text(encoding="utf-8")

        # Check wikilinks: [[Page Name]]
        links = re.findall(r'\[\[([^\]]+)\]\]', text)
        for link in links:
            link_clean = link.split("|")[0] if "|" in link else link
            link_slug = link_clean.lower().replace(" ", "-").replace("/", "-")
            if link_slug not in all_pages and link_clean not in all_labels:
                issues.append(f"BROKEN LINK in {entry['slug']}.md: [[{link_clean}]]")

    # Check for files not in INDEX
    actual_files = set(p.stem for p in wiki_dir.glob("*.md") if p.name != "README.md")
    not_indexed = actual_files - all_pages
    for slug in not_indexed:
        issues.append(f"NOT INDEXED: {slug}.md (exists but not in INDEX.json)")

    if issues:
        print(f"Lint found {len(issues)} issues:")
        for issue in issues[:20]:
            print(f"  !! {issue}")
        if len(issues) > 20:
            print(f"  ... and {len(issues) - 20} more")
    else:
        print(f"Wiki lint: CLEAN ({len(all_pages)} pages, {len(actual_files)} files)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Lint compiled wiki")
    parser.add_argument("--wiki-dir", type=str, default=str(DEFAULT_WIKI))
    args = parser.parse_args()
    lint_wiki(Path(args.wiki_dir))
