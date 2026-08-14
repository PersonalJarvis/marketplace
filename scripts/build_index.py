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
(CommunityIndex / CommunityPluginEntry / CommunitySkillEntry /
CommunityWallpaperEntry).
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
# Where this site itself is served from — wallpaper bytes are linked HERE,
# never into the repo, so publishing survives the repo going private (the
# embed-don't-link lesson, applied to files too big to embed).
_OWNER, _, _REPO_NAME = REPO.partition("/")
PAGES_URL = f"https://{_OWNER.lower()}.github.io/{_REPO_NAME}"

# Wallpaper derivation targets — same numbers as the app's own thumbnailer
# (jarvis/ui/web/wallpapers.py), so a grid tile weighs the same wherever it
# was derived.
WALLPAPER_MAX_WIDTH = 3840
WALLPAPER_QUALITY = 82
THUMB_WIDTH = 480
THUMB_QUALITY = 72

REDIRECT_HTML = f"""<!doctype html>
<meta charset="utf-8">
<meta http-equiv="refresh" content="0; url={STOREFRONT_URL}">
<title>Personal Jarvis Marketplace</title>
<p>The marketplace lives at <a href="{STOREFRONT_URL}">{STOREFRONT_URL}</a>.</p>
"""


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_bundled_skills(plugin_dir: Path) -> list[dict]:
    """The package's skills/<name>/SKILL.md files, embedded verbatim.

    Only SKILL.md travels. A skill may reference other files in the standard,
    but community packages ship instructions, not payloads — see the
    submission rules.
    """
    skills_dir = plugin_dir / "skills"
    if not skills_dir.is_dir():
        return []
    bundled = []
    for skill_dir in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
        skill_md = skill_dir / "SKILL.md"
        if skill_md.exists():
            bundled.append(
                {"name": skill_dir.name, "skill_md": skill_md.read_text(encoding="utf-8")}
            )
    return bundled


def emit_wallpaper(name: str, source: Path, out: Path) -> dict | None:
    """Re-encode one wallpaper into the site; return derived facts.

    The committed file is never served: Pillow decodes it and writes a fresh
    WebP (plus a grid thumbnail), so whatever else the upload carried — EXIF,
    an appended payload, a forged header — does not reach the public site.
    Without Pillow (a local build), the raw file is copied and the thumbnail
    is skipped; the publish workflow always installs Pillow, so the deployed
    site always serves re-encoded bytes.
    """
    target_dir = out / "wallpapers" / name
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image, ImageStat
    except ImportError:
        print(
            f"WARNING: Pillow missing — copying {name} without re-encode/thumbnail",
            file=sys.stderr,
        )
        # Keep the committed container's own name: calling a JPEG
        # "wallpaper.webp" would be a lie every decoder notices.
        (target_dir / source.name).write_bytes(source.read_bytes())
        return {"filename": source.name, "has_thumb": False}

    with Image.open(source) as raw:
        raw.load()
        image = raw.convert("RGB")
    if image.width > WALLPAPER_MAX_WIDTH:
        height = max(1, round(image.height * WALLPAPER_MAX_WIDTH / image.width))
        image = image.resize((WALLPAPER_MAX_WIDTH, height), Image.Resampling.LANCZOS)
    image.save(target_dir / "wallpaper.webp", "WEBP", quality=WALLPAPER_QUALITY, method=4)

    thumb_height = max(1, round(image.height * THUMB_WIDTH / image.width))
    thumb = image.resize((THUMB_WIDTH, thumb_height), Image.Resampling.LANCZOS)
    thumb.save(target_dir / "thumb.webp", "WEBP", quality=THUMB_QUALITY, method=4)

    # Same light/dark heuristic as the app's upload store: mean luminance of
    # a small copy, midpoint threshold.
    mean = ImageStat.Stat(image.convert("L").resize((32, 32))).mean[0]
    return {
        "filename": "wallpaper.webp",
        "width": image.width,
        "height": image.height,
        "theme": "light" if mean >= 128 else "dark",
        "has_thumb": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="_site")
    args = parser.parse_args()
    out = ROOT / args.out

    registry: dict[str, dict] = {}
    registry_path = ROOT / "registry.json"
    if registry_path.exists():
        registry = read_json(registry_path)

    plugins, skills, wallpapers = [], [], []
    for name, meta in sorted(registry.items()):
        if meta.get("kind") == "wallpaper":
            folder = ROOT / "wallpapers" / name
            candidates = [
                folder / filename
                for filename in ("wallpaper.webp", "wallpaper.jpg", "wallpaper.png")
                if (folder / filename).is_file()
            ]
            submission_path = ROOT / "submissions" / f"{name}.json"
            if not candidates or not submission_path.exists():
                continue
            submission = read_json(submission_path)
            derived = emit_wallpaper(name, candidates[0], out)
            emitted = derived["filename"] if derived else "wallpaper.webp"
            entry = {
                "name": name,
                "title": submission.get("title", name),
                "description": submission.get("description", ""),
                "publisher": meta.get("publisher"),
                "version": meta.get("version"),
                "published_at": meta.get("published_at"),
                "license": submission.get("license"),
                "theme": submission.get("theme"),
                "source_url": f"{TREE_URL}/wallpapers/{name}",
                # Bytes are served from THIS site (see PAGES_URL) — the one
                # host that stays up whatever the repo's visibility does.
                "image_url": f"{PAGES_URL}/wallpapers/{name}/{emitted}",
                "thumb_url": f"{PAGES_URL}/wallpapers/{name}/{emitted}",
            }
            if derived and derived.get("width"):
                entry["width"] = derived["width"]
                entry["height"] = derived["height"]
                entry["theme"] = entry["theme"] or derived["theme"]
            if derived and derived.get("has_thumb"):
                entry["thumb_url"] = f"{PAGES_URL}/wallpapers/{name}/thumb.webp"
            wallpapers.append(entry)
        elif meta.get("kind") == "plugin":
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
                    "skills": read_bundled_skills(plugin_dir),
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
                    # The instructions themselves, not a link to them. A
                    # linked file inherits the availability of whatever host
                    # serves it: on 2026-08-14 this repo went private while
                    # Pages kept serving this index, and every raw_url in a
                    # live feed answered 404 — the store listed skills it
                    # could not install. raw_url stays for older clients.
                    "skill_md": skill_path.read_text(encoding="utf-8"),
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
        "wallpapers": wallpapers,
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

    print(
        f"index: {len(plugins)} plugin(s), {len(skills)} skill(s), "
        f"{len(wallpapers)} wallpaper(s) -> {out / 'index.json'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
