#!/usr/bin/env python3
"""The auto-merge gate. Runs from TRUSTED base-branch code on
``pull_request_target`` — it never checks out or executes PR code; it only
downloads the PR's submission file as data and validates it.

A pull request auto-merges when ALL of this holds:
1. It changes exactly ONE file, matching ``submissions/<name>.json``
   (added or modified — never deleted, renamed, or anything else).
2. The submission passes scripts/validate.py (schema, naming, reserved
   names, https-only, launcher allowlist, secret scan, …).
3. The ``publisher`` field equals the PR author's GitHub login.
4. For an update: the publisher on main stays the same (ownership) and the
   version strictly increases — enforced via --base-ref.

Anything else is left open with an explanatory comment for maintainer
review. Validation FAILURES fail the check so the author sees red.

Environment: GH_TOKEN (write), REPO, PR_NUMBER, PR_AUTHOR, HEAD_SHA.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUBMISSION_RE = re.compile(r"^submissions/[a-z0-9][a-z0-9.-]*\.json$")


def gh_api(*args: str) -> str:
    proc = subprocess.run(
        ["gh", "api", *args], capture_output=True, text=True, check=True
    )
    return proc.stdout


def comment(repo: str, pr_number: str, body: str) -> None:
    subprocess.run(
        ["gh", "pr", "comment", pr_number, "--repo", repo, "--body", body],
        check=False,
        capture_output=True,
        text=True,
    )


def main() -> int:
    repo = os.environ["REPO"]
    pr_number = os.environ["PR_NUMBER"]
    pr_author = os.environ["PR_AUTHOR"]
    head_sha = os.environ["HEAD_SHA"]

    files = json.loads(
        gh_api(f"repos/{repo}/pulls/{pr_number}/files", "--paginate")
    )
    paths = [f["filename"] for f in files]
    statuses = {f["filename"]: f["status"] for f in files}

    if len(paths) != 1 or not SUBMISSION_RE.fullmatch(paths[0]):
        comment(
            repo,
            pr_number,
            "This PR touches more than a single `submissions/<name>.json` "
            "file, so it stays open for maintainer review (only one-file "
            "submission PRs merge automatically).",
        )
        print("not eligible: changed-file set")
        return 0
    path = paths[0]
    if statuses[path] not in ("added", "modified"):
        comment(
            repo,
            pr_number,
            f"`{path}` is {statuses[path]} — only added or modified "
            "submissions merge automatically.",
        )
        print("not eligible: file status")
        return 0

    # Fetch the PR's version of the file AS DATA (never execute PR content).
    blob = json.loads(gh_api(f"repos/{repo}/contents/{path}?ref={head_sha}"))
    (ROOT / path).parent.mkdir(parents=True, exist_ok=True)
    (ROOT / path).write_bytes(base64.b64decode(blob["content"]))

    validate = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "validate.py"),
         "--base-ref", "origin/main", str(ROOT / path)],
        capture_output=True,
        text=True,
    )
    print(validate.stdout)
    if validate.returncode != 0:
        comment(
            repo,
            pr_number,
            "Automated validation failed — please fix and push again:\n\n"
            f"```\n{validate.stdout.strip()}\n```",
        )
        print("validation failed")
        return 1

    submission = json.loads((ROOT / path).read_bytes().decode("utf-8"))
    if submission.get("publisher") != pr_author:
        comment(
            repo,
            pr_number,
            f"The submission's `publisher` field is "
            f"`{submission.get('publisher')}` but this PR was opened by "
            f"`{pr_author}`. Set `publisher` to your own GitHub username — "
            "it becomes the ownership record for future updates.",
        )
        print("not eligible: publisher mismatch")
        return 0

    subprocess.run(
        ["gh", "api", "-X", "PUT", f"repos/{repo}/pulls/{pr_number}/merge",
         "-f", "merge_method=squash",
         "-f", f"commit_title=publish: {path} by @{pr_author}"],
        check=True,
        capture_output=True,
        text=True,
    )
    comment(
        repo,
        pr_number,
        "All automated checks passed — merged. The index and storefront "
        "update within a few minutes. Thanks for publishing!",
    )
    print("merged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
