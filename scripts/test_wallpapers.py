#!/usr/bin/env python3
"""Regression test for the wallpapers lane. Stdlib only.

Two halves, matching the lane's two promises:

1. ``validate.py`` settles everything a machine can settle about a wallpaper
   submission — metadata shape, license allowlist, and that the committed
   file beside it is a plausibly-sized WebP.
2. ``automerge_gate.py`` NEVER merges a wallpaper, however valid and however
   trusted the path — a maintainer looks at every image before it publishes,
   because no pattern list recognizes a hateful or illegal picture.

Usage:
    python scripts/test_wallpapers.py
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
NAME = "aurora-drift"
PATH = f"submissions/{NAME}.json"

SUBMISSION = {
    "kind": "wallpaper",
    "name": NAME,
    "publisher": AUTHOR,
    "publisher_id": AUTHOR_ID,
    "version": "1.0.0",
    "title": "Aurora Drift",
    "description": "Northern lights over a quiet fjord.",
    "license": "CC0-1.0",
    "theme": "dark",
}

# The 12-byte RIFF/WEBP container header is all validate.py reads — the full
# decode happens in the publish build, which has Pillow.
WEBP_BYTES = b"RIFF" + (32).to_bytes(4, "little") + b"WEBP" + b"\x00" * 32


def load_module(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_fixture(
    tmp: Path, submission: dict, images: dict[str, bytes] | bytes | None
) -> Path:
    path = tmp / "submissions" / f"{submission['name']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(submission), encoding="utf-8")
    if isinstance(images, bytes):
        images = {"wallpaper.webp": images}
    for filename, payload in (images or {}).items():
        image_path = tmp / "wallpapers" / submission["name"] / filename
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(payload)
    return path


def validate_case(
    title: str,
    submission: dict,
    images: dict[str, bytes] | bytes | None,
    *,
    expect_valid: bool,
) -> bool:
    validate = load_module("validate")
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        validate.ROOT = tmp
        path = write_fixture(tmp, submission, images)
        errors = validate.Errors()
        validate.validate_file(path, errors, None)
    valid = not errors.items
    ok = valid == expect_valid
    verdict = "ok" if ok else f"FAILED (errors={errors.items})"
    print(f"  validate: {title}: {verdict}")
    return ok


class FakeSubprocess:
    """Records argv lists; every call reports success."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, args, **_kwargs):
        self.calls.append(list(args))
        return types.SimpleNamespace(returncode=0, stdout="OK — valid", stderr="")

    def merged(self) -> bool:
        return any("merge" in arg for call in self.calls for arg in call)


def gate_case(title: str, *, pr_author: str, head_repo: str, trusted_bot: str) -> bool:
    """A fully valid wallpaper submission on the given path must NOT merge."""
    gate = load_module("automerge_gate")
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        gate.ROOT = tmp
        gate._PR_CACHE.clear()

        pr = {
            "head": {"repo": {"full_name": head_repo}},
            "user": {"login": pr_author, "id": AUTHOR_ID},
        }
        blob = {"content": base64.b64encode(json.dumps(SUBMISSION).encode()).decode()}
        files = [{"filename": PATH, "status": "added"}]

        def fake_gh_api(*args: str) -> str:
            endpoint = args[0]
            if "/files" in endpoint:
                return json.dumps(files)
            if "/contents/" in endpoint:
                return json.dumps(blob)
            if "/pulls/" in endpoint:
                return json.dumps(pr)
            raise AssertionError(f"unexpected endpoint {endpoint}")

        comments: list[str] = []
        fake_proc = FakeSubprocess()
        gate.gh_api = fake_gh_api
        gate.comment = lambda _r, _n, body: comments.append(body)
        gate.subprocess = fake_proc

        os.environ.update(
            REPO=REPO,
            PR_NUMBER="7",
            PR_AUTHOR=pr_author,
            HEAD_SHA="deadbeef",
            TRUSTED_BOT_LOGIN=trusted_bot,
        )
        gate.main()
        held = not fake_proc.merged()
        explained = any("reviewed lane" in body for body in comments)
    ok = held and explained
    verdict = "ok" if ok else f"FAILED (held={held}, explained={explained})"
    print(f"  gate: {title}: {verdict}")
    return ok


def main() -> int:
    print("wallpaper lane rules:")
    results = [
        validate_case("a complete submission with a WebP image passes",
                      SUBMISSION, WEBP_BYTES, expect_valid=True),
        validate_case("theme may be omitted (build derives it)",
                      {k: v for k, v in SUBMISSION.items() if k != "theme"},
                      WEBP_BYTES, expect_valid=True),
        validate_case("a missing image file is refused",
                      SUBMISSION, None, expect_valid=False),
        validate_case("a file without the WebP header is refused",
                      SUBMISSION, b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, expect_valid=False),
        validate_case("a JPEG arriving as wallpaper.jpg passes (browser without WebP encode)",
                      SUBMISSION, {"wallpaper.jpg": b"\xff\xd8\xff\xe0" + b"\x00" * 64},
                      expect_valid=True),
        validate_case("a PNG arriving as wallpaper.png passes",
                      SUBMISSION, {"wallpaper.png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 64},
                      expect_valid=True),
        validate_case("two image files under one name are refused",
                      SUBMISSION,
                      {"wallpaper.webp": WEBP_BYTES,
                       "wallpaper.jpg": b"\xff\xd8\xff\xe0" + b"\x00" * 64},
                      expect_valid=False),
        validate_case("an image above the byte ceiling is refused",
                      SUBMISSION, WEBP_BYTES + b"\x00" * (8 * 1024 * 1024), expect_valid=False),
        validate_case("a license outside the redistribution allowlist is refused",
                      dict(SUBMISSION, license="proprietary"), WEBP_BYTES, expect_valid=False),
        validate_case("a missing license is refused",
                      {k: v for k, v in SUBMISSION.items() if k != "license"},
                      WEBP_BYTES, expect_valid=False),
        validate_case("a missing title is refused",
                      {k: v for k, v in SUBMISSION.items() if k != "title"},
                      WEBP_BYTES, expect_valid=False),
        validate_case("an unknown theme value is refused",
                      dict(SUBMISSION, theme="sepia"), WEBP_BYTES, expect_valid=False),
        # The load-bearing bit: valid + trusted is still not enough to merge.
        gate_case("a valid wallpaper on the TRUSTED bot path stays open",
                  pr_author=BOT, head_repo=REPO, trusted_bot=BOT),
        gate_case("a valid wallpaper on the fork path stays open",
                  pr_author=AUTHOR, head_repo=f"{AUTHOR}/marketplace", trusted_bot=BOT),
    ]
    if all(results):
        print(f"OK — {len(results)} wallpaper case(s)")
        return 0
    print("FAIL — see cases above")
    return 1


if __name__ == "__main__":
    sys.exit(main())
