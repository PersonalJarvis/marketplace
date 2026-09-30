#!/usr/bin/env python3
"""The auto-merge gate. Runs from TRUSTED base-branch code on
``pull_request_target`` — it never checks out or executes PR code; it only
downloads the PR's submission file as data and validates it.

A pull request auto-merges when ALL of this holds:
1. It changes exactly ONE file, matching ``submissions/<name>.json``
   (added or modified — never deleted, renamed, or anything else).
2. The submission passes scripts/validate.py (schema, naming, reserved
   names, https-only, launcher allowlist, secret scan, …).
3. The publisher identity is proven by ONE of the two trusted paths below.
4. For an update: ownership on main is unchanged and the version strictly
   increases — enforced via --base-ref.

The two paths that prove who published, and nothing else does:

* **Fork path** (a contributor's own pull request): ``publisher`` equals the
  PR author's login, and when ``publisher_id`` is present it equals the PR
  author's numeric account id. The author authenticated to GitHub to open
  the PR, so the author *is* the proof.
* **Trusted-branch path** (the website's upload form, via the GitHub App):
  the head branch lives inside THIS repository — which only the App
  installation and maintainers can write to — and the PR author is the App's
  bot. The endpoint already derived the publisher from a verified GitHub
  session, so the file's publisher fields are trusted, and ``publisher_id``
  is required. Disabled unless TRUSTED_BOT_LOGIN is set, so the path stays
  closed until the App exists.

A fork PR opened by the bot login gets no trust: it takes the fork path and
fails there, because a bot login cannot equal a valid ``publisher``.

Anything else is left open with an explanatory comment for maintainer
review. Validation FAILURES fail the check so the author sees red.

Environment: GH_TOKEN (write), REPO, PR_NUMBER, PR_AUTHOR, HEAD_SHA,
TRUSTED_BOT_LOGIN (optional).
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


_PR_CACHE: dict[str, dict] = {}


def pr_details(repo: str, pr_number: str) -> dict:
    """The pull request object, fetched at most once per run."""
    key = f"{repo}#{pr_number}"
    if key not in _PR_CACHE:
        _PR_CACHE[key] = json.loads(gh_api(f"repos/{repo}/pulls/{pr_number}"))
    return _PR_CACHE[key]


def account_id(doc: dict) -> int | None:
    """The numeric GitHub account id in a submission, or None when absent.

    Mirrors scripts/validate.py: ``bool`` is a subclass of ``int`` in Python,
    so ``true`` must not pass as the id ``1``.
    """
    value = doc.get("publisher_id")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def trusted_branch(repo: str, pr_number: str, pr_author: str) -> bool:
    """True when the head branch lives in THIS repo and our App's bot opened the PR.

    Both halves carry weight. Only the App installation and maintainers can
    push a branch into this repository, and only our upload endpoint drives
    the App — so a branch here that the bot opened is a submission whose
    publisher our server already verified. A fork can never satisfy the
    first half, whatever login it borrows.
    """
    bot = os.environ.get("TRUSTED_BOT_LOGIN", "").strip()
    if not bot or pr_author != bot:
        return False
    head = pr_details(repo, pr_number).get("head") or {}
    return ((head.get("repo") or {}).get("full_name")) == repo


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
    publisher_id = account_id(submission)

    if trusted_branch(repo, pr_number, pr_author):
        # The branch is inside this repo and the App's bot opened the PR, so
        # the publisher fields came from a verified session on our endpoint.
        if publisher_id is None:
            comment(
                repo,
                pr_number,
                "This submission arrived on the trusted path but carries no "
                "`publisher_id`. The upload endpoint always sets it, so this "
                "stays open for maintainer review.",
            )
            print("not eligible: trusted path without publisher_id")
            return 0
    else:
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
        author_id = pr_details(repo, pr_number).get("user", {}).get("id")
        if publisher_id is not None and publisher_id != author_id:
            comment(
                repo,
                pr_number,
                f"The submission's `publisher_id` is `{publisher_id}` but your "
                f"GitHub account id is `{author_id}`. `publisher_id` is the "
                "ownership key — it must be your own numeric account id "
                "(see `https://api.github.com/users/<your-login>`).",
            )
            print("not eligible: publisher_id mismatch")
            return 0

    subprocess.run(
        ["gh", "api", "-X", "PUT", f"repos/{repo}/pulls/{pr_number}/merge",
         "-f", "merge_method=squash",
         # Credit the publisher, not the bot that carried the file.
         "-f", f"commit_title=publish: {path} by @{submission.get('publisher')}"],
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
