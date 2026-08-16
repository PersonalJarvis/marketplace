#!/usr/bin/env python3
"""Regression test for the portability mark on a skill. Stdlib only.

The registry publishes skills for more than one agent. A SKILL.md in the open
Agent Skills format installs into Claude Code, Cursor, Codex and the rest via
`npx skills add`, and the store shows that command beside ours — but only for
the entries that actually are portable. This is what that mark depends on:

- an author never has to declare it: the flavor is read off the file, so every
  submission that predates the field still gets the right mark;
- a file using Jarvis' own keys is "jarvis", one using none of them is
  "portable", and a declared flavor wins over both;
- the "also runs in" list is publisher-written text that lands in the store UI,
  so the feed bounds it and the validator says so instead of truncating
  silently.

Usage:
    python scripts/test_portable_flavor.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

JARVIS_SKILL_MD = """---
schema_version: "1"
name: three-bullet-brief
description: Three bullets and a takeaway
when_to_use: When someone asks for the short version
triggers:
  - type: voice
    pattern: "fasse zusammen"
---

Body.
"""

PORTABLE_SKILL_MD = """---
name: three-point-check
description: Summarize any topic in three bullets
allowed-tools: Read, Grep
model: inherit
---

Body.
"""

MINIMAL_SKILL_MD = """---
name: minimal
description: The two keys the open format asks for
---

Body.
"""


def load(script: str):
    spec = importlib.util.spec_from_file_location(script, ROOT / "scripts" / f"{script}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def main() -> int:
    build_index = load("build_index")
    validate = load("validate")
    failures: list[str] = []

    def check(what: str, condition: bool) -> None:
        if not condition:
            failures.append(what)

    # --- the derivation -----------------------------------------------------
    check(
        "a skill using Jarvis' own keys is jarvis-flavored",
        build_index.skill_flavor({}, JARVIS_SKILL_MD) == "jarvis",
    )
    check(
        "a skill written for another agent is portable",
        build_index.skill_flavor({}, PORTABLE_SKILL_MD) == "portable",
    )
    check(
        "a two-key skill is portable — it runs anywhere a SKILL.md runs",
        build_index.skill_flavor({}, MINIMAL_SKILL_MD) == "portable",
    )
    check(
        "a declared flavor wins over the derivation",
        build_index.skill_flavor({"flavor": "jarvis"}, PORTABLE_SKILL_MD) == "jarvis",
    )
    check(
        "a declared flavor is case- and padding-forgiving",
        build_index.skill_flavor({"flavor": " Portable "}, JARVIS_SKILL_MD) == "portable",
    )
    check(
        "an unknown declared flavor falls back to the derivation",
        build_index.skill_flavor({"flavor": "quantum"}, JARVIS_SKILL_MD) == "jarvis",
    )
    check(
        "a file without frontmatter is not mistaken for a portable skill",
        build_index.skill_flavor({}, "Just a body.\n") == "portable",
    )

    # --- the compatibility list --------------------------------------------
    cleaned = build_index.compatible_agents(
        {
            "compatible_agents": [
                "Claude Code",
                "Claude Code",
                "  Cursor  ",
                "x" * 200,
                42,
                *[f"agent-{i}" for i in range(20)],
            ]
        }
    )
    check("duplicates and non-strings are dropped", cleaned[:2] == ["Claude Code", "Cursor"])
    check("the list is capped", len(cleaned) <= build_index.MAX_COMPATIBLE_AGENTS)
    check(
        "each name is capped",
        all(len(n) <= build_index.MAX_AGENT_NAME_CHARS for n in cleaned),
    )
    check(
        "a non-list degrades to empty",
        build_index.compatible_agents({"compatible_agents": "Cursor"}) == [],
    )

    # --- the validator ------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "submission.json"

        def errors_for(doc: dict) -> list[str]:
            path.write_text(json.dumps(doc), encoding="utf-8")
            errors = validate.Errors()
            validate.validate_flavor(doc, errors, path)
            return errors.items

        check("an absent flavor is fine", errors_for({}) == [])
        check("a known flavor passes", errors_for({"flavor": "portable"}) == [])
        check("an unknown flavor is rejected", errors_for({"flavor": "quantum"}) != [])
        check(
            "a too-long agent list is rejected rather than truncated",
            errors_for({"compatible_agents": [f"agent-{i}" for i in range(20)]}) != [],
        )
        check(
            "an over-long agent name is rejected",
            errors_for({"compatible_agents": ["x" * 200]}) != [],
        )
        check(
            "a non-list agent field is rejected",
            errors_for({"compatible_agents": "Cursor"}) != [],
        )

    for failure in failures:
        print(f"FAIL {failure}")
    if failures:
        return 1
    print("OK - the portability mark is derived, declarable, and bounded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
