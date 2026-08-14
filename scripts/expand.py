#!/usr/bin/env python3
"""Expand validated submissions into the published registry layout.

Runs on main after every merge (publish workflow). For each
``submissions/<name>.json``:

- kind=plugin → ``plugins/<name>/plugin.json`` (+ ``mcp.json``,
  ``io.github.personaljarvis/usage-card.md``) per the Agent Plugins v1.0.0
  packaging standard.
- kind=skill  → ``skills/<name>/SKILL.md`` (the file the app downloads).

``registry.json`` records name → {kind, publisher, version, published_at};
it is the ownership ledger the auto-merge gate enforces. ``published_at`` is
stamped on first publish and refreshed when the version changes.

Idempotent: a second run with no new submissions changes nothing, so the
publish workflow cannot loop on its own expansion commit.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXTENSION_DIR = "io.github.personaljarvis"


def write_if_changed(path: Path, content: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return True


def dump(document: object) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def main() -> int:
    registry_path = ROOT / "registry.json"
    registry: dict[str, dict] = {}
    if registry_path.exists():
        registry = json.loads(registry_path.read_text(encoding="utf-8"))

    changed = False
    for path in sorted((ROOT / "submissions").glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        name, kind = doc["name"], doc["kind"]

        if kind == "plugin":
            target = ROOT / "plugins" / name
            changed |= write_if_changed(target / "plugin.json", dump(doc["plugin_json"]))
            if doc.get("mcp_json"):
                changed |= write_if_changed(target / "mcp.json", dump(doc["mcp_json"]))
            # `skills/<name>/SKILL.md` — the layout the Agent Plugins standard
            # defines, so the published directory is a real package another
            # client can read, not a shape only this registry understands.
            # Names (and path traversal) are settled by validate.py before
            # anything reaches here.
            for skill in doc.get("skills") or []:
                changed |= write_if_changed(
                    target / "skills" / skill["name"] / "SKILL.md", skill["skill_md"]
                )
            if doc.get("usage_card"):
                changed |= write_if_changed(
                    target / EXTENSION_DIR / "usage-card.md", doc["usage_card"]
                )
        elif kind == "wallpaper":
            # Nothing to derive: the approve flow committed the image at
            # wallpapers/<name>/wallpaper.webp beside the submission, and the
            # site build re-encodes it into _site. Only the ledger moves.
            pass
        else:
            changed |= write_if_changed(ROOT / "skills" / name / "SKILL.md", doc["skill_md"])

        previous = registry.get(name) or {}
        entry = {"kind": kind, "publisher": doc["publisher"], "version": doc["version"]}
        if doc.get("publisher_id") is not None:
            entry["publisher_id"] = doc["publisher_id"]
        # published_at marks the release, so it only moves when the version
        # does — backfilling publisher_id must not restamp a live entry.
        if previous.get("version") == doc["version"] and previous.get("published_at"):
            entry["published_at"] = previous["published_at"]
        else:
            entry["published_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        if previous != entry:
            registry[name] = entry
            changed = True

    changed |= write_if_changed(registry_path, dump(registry))
    print("expanded: changes written" if changed else "expanded: nothing to do")
    return 0


if __name__ == "__main__":
    sys.exit(main())
