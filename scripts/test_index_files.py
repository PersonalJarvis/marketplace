#!/usr/bin/env python3
"""Regression test for the file listing the feed carries. Stdlib only.

Every published entry travels with its whole folder — path, size and verbatim
text per file — so the storefront can show a visitor what is inside a package
before they install it. The rules below are what that promise depends on:

- nothing is left out, including nested and dot-files;
- a file that is not UTF-8 text still appears, with its size and no text,
  rather than breaking the build or silently vanishing;
- oversized files are listed but not embedded, so one bad entry cannot bloat
  the feed every client downloads;
- root files come before deeper ones, which is the order the tree is drawn in.

Usage:
    python scripts/test_index_files.py
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_build_index():
    spec = importlib.util.spec_from_file_location(
        "build_index", ROOT / "scripts" / "build_index.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def main() -> int:
    build_index = load_build_index()
    failures: list[str] = []

    def check(what: str, condition: bool) -> None:
        if not condition:
            failures.append(what)

    with tempfile.TemporaryDirectory() as tmp:
        entry = Path(tmp) / "demo-plugin"
        (entry / "io.github.personaljarvis").mkdir(parents=True)
        # write_bytes, not write_text: text mode would rewrite "\n" as the
        # platform's line ending, and this test asserts on exact content.
        (entry / "plugin.json").write_bytes(b'{"name": "demo"}\n')
        (entry / "mcp.json").write_bytes(b'{"mcpServers": {}}\n')
        (entry / ".gitattributes").write_bytes(b"* text=auto\n")
        (entry / "io.github.personaljarvis" / "usage-card.md").write_bytes(
            b"Use it for demos.\n"
        )
        (entry / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe")
        (entry / "huge.md").write_bytes(b"x" * (build_index.MAX_EMBED_BYTES + 1))

        files = build_index.collect_files(entry)
        by_path = {f["path"]: f for f in files}

        check(
            "every file is listed, nested and dot-files included",
            set(by_path) == {
                ".gitattributes",
                "huge.md",
                "logo.png",
                "mcp.json",
                "plugin.json",
                "io.github.personaljarvis/usage-card.md",
            },
        )
        check(
            "text files travel verbatim",
            by_path["plugin.json"]["text"] == '{"name": "demo"}\n',
        )
        check(
            "a nested file keeps its posix path",
            by_path["io.github.personaljarvis/usage-card.md"]["text"] == "Use it for demos.\n",
        )
        check("a binary file is listed without text", by_path["logo.png"]["text"] is None)
        check("a binary file keeps its real size", by_path["logo.png"]["size"] == 10)
        check("an oversized file is listed without text", by_path["huge.md"]["text"] is None)
        check(
            "an oversized file keeps its real size",
            by_path["huge.md"]["size"] == build_index.MAX_EMBED_BYTES + 1,
        )
        check(
            "root files come before nested ones",
            [f["path"] for f in files][-1] == "io.github.personaljarvis/usage-card.md",
        )

        empty = Path(tmp) / "empty"
        empty.mkdir()
        check("an empty folder yields an empty list", build_index.collect_files(empty) == [])

    for failure in failures:
        print(f"FAIL {failure}")
    if failures:
        return 1
    print("OK - published files are listed completely and honestly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
