#!/usr/bin/env python3
"""Compile the published registry into the static site the app consumes.

Output (default ``_site/``):
- ``index.json``  — the single feed Personal Jarvis fetches (community
  plugins with embedded manifests + skills with raw download URLs). The
  storefront on personaljarvis.ai reads the SAME file client-side.
- ``rules.json``  — the generated submission rules, published so the upload
  endpoint and the app can read the limits instead of retyping them
  (scripts/export_rules.py).
- ``index.html``  — a small redirect to the storefront for humans who open
  the Pages URL directly.

Keep the wire shape in sync with jarvis/marketplace/community_source.py
(CommunityIndex / CommunityPluginEntry / CommunitySkillEntry).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXTENSION_DIR = "io.github.personaljarvis"

REPO = os.environ.get("GITHUB_REPOSITORY", "PersonalJarvis/marketplace")
BRANCH = "main"
TREE_URL = f"https://github.com/{REPO}/tree/{BRANCH}"
RAW_URL = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}"
STOREFRONT_URL = "https://personaljarvis.ai/marketplace"

REDIRECT_HTML = f"""<!doctype html>
<meta charset="utf-8">
<meta http-equiv="refresh" content="0; url={STOREFRONT_URL}">
<title>Personal Jarvis Marketplace</title>
<p>The marketplace lives at <a href="{STOREFRONT_URL}">{STOREFRONT_URL}</a>.</p>
"""


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="_site")
    args = parser.parse_args()
    out = ROOT / args.out

    registry: dict[str, dict] = {}
    registry_path = ROOT / "registry.json"
    if registry_path.exists():
        registry = read_json(registry_path)

    plugins, skills = [], []
    for name, meta in sorted(registry.items()):
        if meta.get("kind") == "plugin":
            plugin_dir = ROOT / "plugins" / name
            plugin_json_path = plugin_dir / "plugin.json"
            if not plugin_json_path.exists():
                continue
            mcp_path = plugin_dir / "mcp.json"
            card_path = plugin_dir / EXTENSION_DIR / "usage-card.md"
            plugins.append(
                {
                    "name": name,
                    "publisher": meta.get("publisher"),
                    "version": meta.get("version"),
                    "published_at": meta.get("published_at"),
                    "source_url": f"{TREE_URL}/plugins/{name}",
                    "plugin_json": read_json(plugin_json_path),
                    "mcp_json": read_json(mcp_path) if mcp_path.exists() else None,
                    "usage_card": (
                        card_path.read_text(encoding="utf-8") if card_path.exists() else None
                    ),
                }
            )
        else:
            skill_path = ROOT / "skills" / name / "SKILL.md"
            if not skill_path.exists():
                continue
            submission = read_json(ROOT / "submissions" / f"{name}.json")
            skills.append(
                {
                    "name": name,
                    "title": submission.get("title", name),
                    "description": submission.get("description", ""),
                    "publisher": meta.get("publisher"),
                    "version": meta.get("version"),
                    "published_at": meta.get("published_at"),
                    "categories": submission.get("categories", []),
                    "source_url": f"{TREE_URL}/skills/{name}",
                    "raw_url": f"{RAW_URL}/skills/{name}/SKILL.md",
                }
            )

    index = {
        # Monotonic enough for cache-busting: the workflow run number, or a
        # timestamp when built locally.
        "revision": int(os.environ.get("GITHUB_RUN_NUMBER", 0))
        or int(datetime.now(UTC).timestamp()),
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "plugins": plugins,
        "skills": skills,
    }

    out.mkdir(parents=True, exist_ok=True)
    (out / "index.json").write_text(
        json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (out / "index.html").write_text(REDIRECT_HTML, encoding="utf-8")

    # Ship the generated rules beside the feed so the upload endpoint and the
    # app read the limits rather than retyping them. The index still deploys
    # without it — a missing feed is worse than a missing rule sheet.
    rules_path = ROOT / "rules.json"
    if rules_path.exists():
        (out / "rules.json").write_text(rules_path.read_text(encoding="utf-8"), encoding="utf-8")
    else:
        print("WARNING: rules.json missing — run scripts/export_rules.py", file=sys.stderr)

    print(f"index: {len(plugins)} plugin(s), {len(skills)} skill(s) -> {out / 'index.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
