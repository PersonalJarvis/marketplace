#!/usr/bin/env python3
"""Regression test for the ownership rule. Stdlib only — no dependencies.

Ownership is the one rule that cannot be re-checked later: once a name is
merged under the wrong account, every future update belongs to whoever
holds it. So the cases below are pinned here rather than proven by hand.

The point of `publisher_id` is that a GitHub login can be renamed and the
freed name re-registered by a stranger, who would otherwise inherit every
entry published under it. The numeric account id never changes, so it is
the ownership key and the login is display text.

`read_base_version` is stubbed with the entry as it would exist on main,
which keeps the test free of git plumbing and of the live submissions.

Usage:
    python scripts/test_ownership.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OWNER_ID = 4242
OWNER_LOGIN = "original-owner"

BASE_ENTRY = {
    "kind": "skill",
    "name": "demo-skill",
    "publisher": OWNER_LOGIN,
    "publisher_id": OWNER_ID,
    "version": "1.0.0",
}

SKILL_MD = (
    "---\nschema_version: \"1\"\nname: demo-skill\nversion: \"1.0.0\"\n"
    "description: A fixture skill used by the ownership test.\n---\n\nBody.\n"
)

SUBMISSION = dict(
    BASE_ENTRY,
    title="Demo Skill",
    description="A fixture skill used by the ownership test.",
    categories=["testing"],
    skill_md=SKILL_MD,
)

# (title, patch applied to the submission, base entry on main, expected)
CASES: list[tuple[str, dict, dict | None, str]] = [
    (
        "hijack: same login string, different account id",
        {"publisher_id": 999999, "version": "1.0.1"},
        BASE_ENTRY,
        "reject",
    ),
    (
        "bypass: publisher_id omitted on an update that needs it",
        {"publisher_id": None, "version": "1.0.1"},
        BASE_ENTRY,
        "reject",
    ),
    (
        "invalid: publisher_id is true (bool subclasses int in Python)",
        {"publisher_id": True, "version": "1.0.1"},
        BASE_ENTRY,
        "reject",
    ),
    (
        "invalid: publisher_id is a string",
        {"publisher_id": "4242", "version": "1.0.1"},
        BASE_ENTRY,
        "reject",
    ),
    (
        "legitimate: same id, higher version",
        {"version": "1.0.1"},
        BASE_ENTRY,
        "accept",
    ),
    (
        "legitimate: owner renamed their GitHub login, id unchanged",
        {"publisher": "renamed-owner", "version": "1.0.1"},
        BASE_ENTRY,
        "accept",
    ),
    (
        "stale: version not increased",
        {"version": "1.0.0"},
        BASE_ENTRY,
        "reject",
    ),
    (
        "legacy: entry predating publisher_id still compares logins",
        {"publisher_id": None, "version": "1.0.1"},
        {k: v for k, v in BASE_ENTRY.items() if k != "publisher_id"},
        "accept",
    ),
    (
        "legacy hijack: entry without an id, different login",
        {"publisher": "someone-else", "publisher_id": None, "version": "1.0.1"},
        {k: v for k, v in BASE_ENTRY.items() if k != "publisher_id"},
        "reject",
    ),
    (
        "first publish: no entry on main yet",
        {},
        None,
        "accept",
    ),
]


def load_validator():
    spec = importlib.util.spec_from_file_location("validate", ROOT / "scripts" / "validate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    validate = load_validator()
    failures = 0

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "demo-skill.json"
        for title, patch, base, expected in CASES:
            doc = dict(SUBMISSION)
            for key, value in patch.items():
                if value is None:
                    doc.pop(key, None)
                else:
                    doc[key] = value
            target.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

            validate.read_base_version = lambda _path, _ref, _base=base: _base
            errors = validate.Errors()
            validate.validate_file(target, errors, "origin/main")

            actual = "reject" if errors.items else "accept"
            ok = actual == expected
            failures += not ok
            print(f"[{'PASS' if ok else 'FAIL'}] {title} — expected {expected}, got {actual}")
            for item in errors.items:
                print(f"           {item.split(': ', 1)[-1]}")

    print()
    if failures:
        print(f"FAIL — {failures} of {len(CASES)} ownership case(s) wrong")
        return 1
    print(f"OK — {len(CASES)} ownership cases hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
