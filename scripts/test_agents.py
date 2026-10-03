#!/usr/bin/env python3
"""Regression test: what an agent template may and may not carry.

An agent template is installed on a stranger's machine with no human review,
so the refusals are the feature. Pinned here:

- a clean template passes;
- every field that names the author's machine (account, workspace, computer)
  or widens what runs without asking (ceiling, approval bypass, always-allow,
  budget, browser attach) is refused, with its reason;
- unknown fields are refused — the template is an allowlist;
- a home folder in the instructions is refused (the author's real name);
- a credential anywhere is refused;
- an imported figure (`avatar.model`, a URL on the author's machine) is refused;
- "Jarvis" cannot be published — every install already has its lead;
- an update may not turn a skill's name into an agent (kind is fixed).

Usage:
    python scripts/test_agents.py
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_validator():
    spec = importlib.util.spec_from_file_location("validate", ROOT / "scripts" / "validate.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


BASE = {
    "kind": "agent",
    "name": "inbox-butler",
    "version": "1.0.0",
    "publisher": "octocat",
    "publisher_id": 583231,
    "title": "Inbox Butler",
    "description": "Sorts the inbox every morning and drafts the answers that matter.",
    "categories": ["productivity"],
    "agent": {
        "schema": 1,
        "name": "Inbox Butler",
        "title": "Keeps the inbox at zero",
        "instructions": "You own the inbox. Every morning, sort new mail into Action, "
        "Waiting and Read later. Draft answers; never send without approval.",
        "tier": "specialist",
        "effort": "medium",
        "focus": ["plugin:gmail", "core:browser"],
        "grant_mode": "all",
        "grants": [],
        "denies": [],
        "skills": None,
        "require_approval": ["plugin:gmail:send"],
        "knowledge_scope": "shared",
        "avatar": {
            "contract": 1,
            "archetype": "biped",
            "base": "rogue",
            "parts": {"head": "hood"},
            "palette": {"skin": "#f4b68f"},
            "companion": {"shape": "circle", "color": "#3366ff"},
        },
    },
}


def problems(validator, doc: dict) -> list[str]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"{doc.get('name')}.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        errors = validator.Errors()
        validator.validate_file(path, errors, None)
        return errors.items


def main() -> int:
    v = load_validator()
    failures: list[str] = []

    def expect_ok(label: str, doc: dict) -> None:
        found = problems(v, doc)
        if found:
            failures.append(f"{label}: expected valid, got {found}")

    def expect_refused(label: str, doc: dict, needle: str) -> None:
        found = problems(v, doc)
        if not any(needle in item for item in found):
            failures.append(f"{label}: expected a refusal mentioning {needle!r}, got {found}")

    expect_ok("clean template", BASE)

    for key, reason in v.AGENT_FORBIDDEN_KEYS.items():
        doc = copy.deepcopy(BASE)
        doc["agent"][key] = "x"
        expect_refused(f"forbidden {key}", doc, reason)

    doc = copy.deepcopy(BASE)
    doc["agent"]["notes"] = "x"
    expect_refused("unknown field", doc, "agent.notes is not a template field")

    for path in ("C:\\Users\\ruben\\Documents", "/Users/ruben/notes", "/home/ruben/x"):
        doc = copy.deepcopy(BASE)
        doc["agent"]["instructions"] += f" Files live in {path}."
        expect_refused(f"home path {path}", doc, "name a folder on your machine")

    doc = copy.deepcopy(BASE)
    doc["agent"]["instructions"] += " Use key sk-abcdefghijklmnopqrstuvwxyz123456."
    expect_refused("credential", doc, "credential pattern")

    doc = copy.deepcopy(BASE)
    doc["agent"]["avatar"]["model"] = "/api/society/figures/abc.glb"
    expect_refused("imported figure", doc, "agent.avatar.model may not be published")

    doc = copy.deepcopy(BASE)
    doc["agent"]["name"] = "Jarvis"
    expect_refused("lead name", doc, "lead every install already has")

    doc = copy.deepcopy(BASE)
    doc["agent"]["tier"] = "lead"
    expect_refused("lead tier", doc, "agent.tier must be one of")

    doc = copy.deepcopy(BASE)
    doc["agent"]["focus"] = ["gmail; rm -rf /"]
    expect_refused("bad capability id", doc, "is not a valid id")

    doc = copy.deepcopy(BASE)
    doc["agent"]["instructions"] = "x" * (v.MAX_AGENT_INSTRUCTIONS_BYTES + 1)
    expect_refused("instructions too large", doc, "agent.instructions larger than")

    doc = copy.deepcopy(BASE)
    del doc["agent"]["instructions"]
    expect_refused("instructions missing", doc, "standing instructions) are required")

    doc = copy.deepcopy(BASE)
    doc["agent"]["schema"] = 2
    expect_refused("schema", doc, "agent.schema must be 1")

    doc = copy.deepcopy(BASE)
    doc["description"] = ""
    expect_refused("summary missing", doc, "description is required for kind=agent")

    # kind may not change on an update: a skill name stays a skill.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "inbox-butler.json"
        newer = copy.deepcopy(BASE)
        newer["version"] = "2.0.0"
        path.write_text(json.dumps(newer), encoding="utf-8")
        errors = v.Errors()
        original = v.read_base_version
        v.read_base_version = lambda _p, _r: {**BASE, "kind": "skill"}
        try:
            v.validate_file(path, errors, "HEAD")
        finally:
            v.read_base_version = original
        if not any("kind may not change" in item for item in errors.items):
            failures.append(f"kind change: expected a refusal, got {errors.items}")

    if failures:
        print("FAIL")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("OK — agent template rules hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
