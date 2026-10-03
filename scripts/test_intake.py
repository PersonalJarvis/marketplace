#!/usr/bin/env python3
"""Regression test: the issue intake publishes only what it may, as whom it may.

The intake turns an issue into a published entry with no human in the loop,
so its identity rule is pinned above all: the publisher is the account that
OPENED the issue — never what the JSON inside it claims. Also pinned:

- only issues tagged as submissions are touched;
- the JSON is found in the fenced block, the way both the app and the issue
  form write it, and a body without one is refused with a sentence;
- bots and organisations cannot publish;
- a stranger cannot update an entry somebody else owns by writing the
  owner's name and id into the file (end to end, in --dry-run);
- a valid new submission would publish, and the dry run leaves the checkout
  exactly as it found it.

Usage:
    python scripts/test_intake.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_intake():
    spec = importlib.util.spec_from_file_location("intake", ROOT / "scripts" / "intake.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


AGENT = {
    "kind": "agent",
    "name": "intake-test-agent",
    "version": "1.0.0",
    "title": "Intake Test",
    "description": "A template used only by the intake regression test.",
    "agent": {
        "schema": 1,
        "name": "Intake Test",
        "title": "Tests the intake",
        "instructions": "You exist to prove the intake works.",
    },
}

USER = {"login": "octocat", "id": 583231, "type": "User"}


def body_for(doc: dict) -> str:
    return (
        "<!-- jarvis-marketplace-submission -->\n### Submission\n\n```json\n"
        + json.dumps(doc, indent=2)
        + "\n```\n"
    )


def dry_run(issue: dict) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        event = Path(tmp) / "event.json"
        event.write_text(
            json.dumps({"issue": issue, "repository": {"full_name": "x/y"}}), encoding="utf-8"
        )
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "intake.py"), "--dry-run",
             "--event", str(event)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=ROOT,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        return proc.stdout + proc.stderr


def main() -> int:
    intake = load_intake()
    failures: list[str] = []

    def check(label: str, condition: bool) -> None:
        if not condition:
            failures.append(label)

    # Which issues are submissions.
    check("title tag", intake.is_submission({"title": "[publish] my agent", "body": ""}))
    check("body marker", intake.is_submission({"title": "x", "body": intake.BODY_MARKER}))
    check("bug report ignored", not intake.is_submission({"title": "Bug", "body": "broken"}))

    # Extraction.
    doc = intake.extract_submission(body_for(AGENT))
    check("extracts the fenced JSON", doc == AGENT)
    for bad, needle in (("no fences here", "no JSON code block"),
                        ("```json\n{nope\n```", "not valid JSON"),
                        ("```json\n[1, 2]\n```", "one JSON object")):
        try:
            intake.extract_submission(bad)
            failures.append(f"refusal expected for {bad!r}")
        except intake.Refusal as exc:
            check(f"refusal names the problem ({needle})", needle in str(exc))

    # Identity: the issue author wins over the JSON.
    forged = {**AGENT, "publisher": "rubenluetke10-beep", "publisher_id": 226271791}
    stamped = intake.stamp_publisher(forged, USER)
    check("publisher taken from the author", stamped["publisher"] == "octocat")
    check("publisher_id taken from the author", stamped["publisher_id"] == 583231)
    for user in ({**USER, "type": "Bot"}, {**USER, "type": "Organization"}):
        try:
            intake.stamp_publisher(AGENT, user)
            failures.append(f"{user['type']} must not publish")
        except intake.Refusal:
            pass

    before = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True,
                            cwd=ROOT).stdout

    # End to end: a valid new submission would publish.
    out = dry_run({"number": 1, "title": "[publish] intake test", "state": "open",
                   "body": body_for(AGENT), "user": USER, "labels": []})
    check(f"valid submission would publish: {out}", "would publish intake-test-agent" in out)

    # End to end: a stranger cannot take over an owned entry by forging the owner.
    owned = json.loads((ROOT / "submissions" / "humanizer.json").read_text(encoding="utf-8"))
    takeover = {**owned, "version": "9.9.9"}
    out = dry_run({"number": 2, "title": "[publish] humanizer", "state": "open",
                   "body": body_for(takeover), "user": USER, "labels": []})
    check(f"takeover refused: {out}", "publisher_id may not change" in out)
    check("takeover not published", "would publish" not in out)

    after = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True,
                           cwd=ROOT).stdout
    check("dry runs leave the checkout untouched", before == after)

    if failures:
        print("FAIL")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("OK — issue intake rules hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
