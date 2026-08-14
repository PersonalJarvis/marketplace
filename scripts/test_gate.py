#!/usr/bin/env python3
"""Regression test for the auto-merge decision. Stdlib only.

The gate merges without a human looking, so both of its trust paths are
worth pinning — especially the trusted-branch one, which accepts the
submission's own `publisher` field and would be the whole ballgame if it
could be reached from a fork.

GitHub is stubbed: `gh_api`, `comment` and the subprocess calls are replaced,
and ROOT is redirected into a temporary directory, so no request leaves the
machine and the live submissions are never touched. What the test asserts is
one bit — did the gate call the merge endpoint.

Usage:
    python scripts/test_gate.py
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

REPO = "PersonalJarvis/marketplace"
BOT = "personal-jarvis-marketplace[bot]"
AUTHOR = "some-contributor"
AUTHOR_ID = 4242
PATH = "submissions/demo-skill.json"

SUBMISSION = {
    "kind": "skill",
    "name": "demo-skill",
    "publisher": AUTHOR,
    "publisher_id": AUTHOR_ID,
    "version": "1.0.0",
    "title": "Demo Skill",
    "description": "Fixture.",
    "skill_md": "---\nname: demo-skill\n---\n\nBody.\n",
}


def load_gate():
    spec = importlib.util.spec_from_file_location("gate", ROOT / "scripts" / "automerge_gate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeSubprocess:
    """Records argv lists; every call reports success."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, args, **_kwargs):
        self.calls.append(list(args))
        return types.SimpleNamespace(returncode=0, stdout="OK — valid", stderr="")

    def merged(self) -> bool:
        return any("merge" in arg for call in self.calls for arg in call)


def run_case(
    gate,
    tmp: Path,
    *,
    submission: dict,
    pr_author: str,
    head_repo: str | None,
    trusted_bot: str,
    files: list[dict],
) -> bool:
    """Drive gate.main() once; return True when it merged."""
    gate.ROOT = tmp
    gate._PR_CACHE.clear()

    pr = {
        "head": {"repo": {"full_name": head_repo} if head_repo else None},
        "user": {"login": pr_author, "id": AUTHOR_ID},
    }
    blob = {"content": base64.b64encode(json.dumps(submission).encode()).decode()}

    def fake_gh_api(*args: str) -> str:
        endpoint = args[0]
        if "/files" in endpoint:
            return json.dumps(files)
        if "/contents/" in endpoint:
            return json.dumps(blob)
        if "/pulls/" in endpoint:
            return json.dumps(pr)
        raise AssertionError(f"unexpected endpoint {endpoint}")

    fake_proc = FakeSubprocess()
    gate.gh_api = fake_gh_api
    gate.comment = lambda *_a, **_k: None
    gate.subprocess = fake_proc

    os.environ.update(
        REPO=REPO,
        PR_NUMBER="7",
        PR_AUTHOR=pr_author,
        HEAD_SHA="deadbeef",
        TRUSTED_BOT_LOGIN=trusted_bot,
    )
    gate.main()
    return fake_proc.merged()


ONE_FILE = [{"filename": PATH, "status": "added"}]


def cases() -> list[tuple[str, dict, str]]:
    """(title, kwargs for run_case, expected verdict)."""
    bot_submission = dict(SUBMISSION, publisher="someone-the-endpoint-verified")
    return [
        (
            "trusted: bot opened a branch inside the repo, publisher_id present",
            {
                "submission": bot_submission,
                "pr_author": BOT,
                "head_repo": REPO,
                "trusted_bot": BOT,
                "files": ONE_FILE,
            },
            "merge",
        ),
        (
            "trusted path is unreachable from a fork, even using the bot login",
            {
                "submission": bot_submission,
                "pr_author": BOT,
                "head_repo": "attacker/marketplace",
                "trusted_bot": BOT,
                "files": ONE_FILE,
            },
            "hold",
        ),
        (
            "trusted branch but publisher_id missing — the endpoint always sets it",
            {
                "submission": {k: v for k, v in bot_submission.items() if k != "publisher_id"},
                "pr_author": BOT,
                "head_repo": REPO,
                "trusted_bot": BOT,
                "files": ONE_FILE,
            },
            "hold",
        ),
        (
            "trusted path stays closed while TRUSTED_BOT_LOGIN is unset",
            {
                "submission": bot_submission,
                "pr_author": BOT,
                "head_repo": REPO,
                "trusted_bot": "",
                "files": ONE_FILE,
            },
            "hold",
        ),
        (
            "fork: publisher and publisher_id both match the author",
            {
                "submission": SUBMISSION,
                "pr_author": AUTHOR,
                "head_repo": f"{AUTHOR}/marketplace",
                "trusted_bot": BOT,
                "files": ONE_FILE,
            },
            "merge",
        ),
        (
            "fork: publisher_id belongs to somebody else",
            {
                "submission": dict(SUBMISSION, publisher_id=999999),
                "pr_author": AUTHOR,
                "head_repo": f"{AUTHOR}/marketplace",
                "trusted_bot": BOT,
                "files": ONE_FILE,
            },
            "hold",
        ),
        (
            "fork: publisher claims a different login",
            {
                "submission": dict(SUBMISSION, publisher="victim"),
                "pr_author": AUTHOR,
                "head_repo": f"{AUTHOR}/marketplace",
                "trusted_bot": BOT,
                "files": ONE_FILE,
            },
            "hold",
        ),
        (
            "fork: legacy submission with no publisher_id still merges",
            {
                "submission": {k: v for k, v in SUBMISSION.items() if k != "publisher_id"},
                "pr_author": AUTHOR,
                "head_repo": f"{AUTHOR}/marketplace",
                "trusted_bot": BOT,
                "files": ONE_FILE,
            },
            "merge",
        ),
        (
            "two files touched",
            {
                "submission": SUBMISSION,
                "pr_author": AUTHOR,
                "head_repo": f"{AUTHOR}/marketplace",
                "trusted_bot": BOT,
                "files": ONE_FILE + [{"filename": "README.md", "status": "modified"}],
            },
            "hold",
        ),
        (
            "submission deleted rather than added",
            {
                "submission": SUBMISSION,
                "pr_author": AUTHOR,
                "head_repo": f"{AUTHOR}/marketplace",
                "trusted_bot": BOT,
                "files": [{"filename": PATH, "status": "removed"}],
            },
            "hold",
        ),
    ]


def main() -> int:
    gate = load_gate()
    failures = 0

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        for title, kwargs, expected in cases():
            merged = run_case(gate, tmp, **kwargs)
            actual = "merge" if merged else "hold"
            ok = actual == expected
            failures += not ok
            print(f"[{'PASS' if ok else 'FAIL'}] {title} — expected {expected}, got {actual}")

    print()
    if failures:
        print(f"FAIL — {failures} of {len(cases())} gate case(s) wrong")
        return 1
    print(f"OK — {len(cases())} gate cases hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
