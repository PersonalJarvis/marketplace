#!/usr/bin/env python3
"""Validate marketplace submission files. Stdlib only — no dependencies.

This is the automated half of the registry's trust model: submissions that
pass merge WITHOUT human review, so every rule here is load-bearing. The
Personal Jarvis app re-enforces the same rules at install time
(jarvis/marketplace/agent_plugins_loader.py) — keep the two in sync.

Usage:
    python scripts/validate.py [FILE...]           # default: submissions/*.json
    python scripts/validate.py --base-ref origin/main FILE...

With --base-ref, a submission that already exists at that git ref must keep
its publisher and strictly increase its version (the anti-hijack rule).

Ownership is keyed on ``publisher_id`` — the numeric GitHub account id —
whenever the existing entry carries one. GitHub logins can be renamed, and
the freed name can then be claimed by anyone, who would inherit every entry
published under it; the numeric id never changes. Entries predating the
field fall back to the login comparison until their next update.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXTENSION_NAMESPACE = "io.github.personaljarvis"
PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"

MAX_FILE_BYTES = 128 * 1024
MAX_SKILL_BYTES = 64 * 1024
MAX_DESCRIPTION_CHARS = 500

# Agent Plugins v1.0.0 name rules.
NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,62}[a-z0-9])?$")
GITHUB_LOGIN_RE = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9]|-(?=[a-zA-Z0-9])){0,38}$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
ENV_PLACEHOLDER_RE = re.compile(r"^\$(?:plugin_[a-z0-9_.-]+|\{plugin_[a-z0-9_.-]+\})$")
HEADER_PLACEHOLDER_RE = re.compile(r"\$(?:plugin_[a-z0-9_.-]+|\{plugin_[a-z0-9_.-]+\})")
TOKEN_LITERAL_RE = re.compile(r"[A-Za-z0-9_\-]{20,}")

STDIO_LAUNCHERS = ("npx", "uvx", "docker")

AUTH_MODES = (
    "pat_paste",
    "oauth_device_flow",
    "hosted_mcp_oauth_dcr",
    "oauth_pkce_loopback",
    "hosted_mcp_allowlist",
)

# Names the shipped app already uses. A community submission must never
# claim one of these: the app refuses the install, so the listing would only
# mislead. Includes the spec-conformant renames.
RESERVED_PLUGIN_IDS = frozenset(
    {
        "github", "vercel", "supabase", "notion", "slack", "linear", "stripe",
        "cloudflare", "discord", "telegram", "asana", "gmail", "todoist",
        "clickup", "dropbox", "canva", "airtable",
        "google_drive", "google-drive", "google_calendar", "google-calendar",
        "cal_com", "cal-com", "home_assistant", "home-assistant",
    }
)
RESERVED_SKILL_NAMES = frozenset(
    {
        "cli-gcloud", "control-api", "deep-work-mode", "jarvis-doc-author",
        "memory-save", "morning-routine", "skill-creator",
    }
    | {f"plugin-{p}" for p in RESERVED_PLUGIN_IDS}
)

# Obvious credential shapes. Matched against the RAW submission text so a
# token can not hide anywhere (headers, markdown, nested manifest fields).
SECRET_PATTERNS = (
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"gho_[A-Za-z0-9]{20,}"),
    re.compile(r"sk-[A-Za-z0-9\-_]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"AIza[0-9A-Za-z\-_]{30,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{20,}\.eyJ[A-Za-z0-9_\-]{20,}"),  # JWT
)


class Errors:
    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, path: Path, message: str) -> None:
        self.items.append(f"{path.as_posix()}: {message}")


def reject_http_urls(value, where: str, errors: Errors, path: Path) -> None:
    if isinstance(value, str):
        if value.lstrip().lower().startswith("http://"):
            errors.add(path, f"{where}: non-https URL {value!r}")
    elif isinstance(value, dict):
        for key, sub in value.items():
            reject_http_urls(sub, f"{where}.{key}", errors, path)
    elif isinstance(value, list):
        for index, sub in enumerate(value):
            reject_http_urls(sub, f"{where}[{index}]", errors, path)


def validate_mcp_json(name: str, mcp: dict, errors: Errors, path: Path) -> None:
    servers = mcp.get("mcpServers")
    if not isinstance(servers, dict) or not servers:
        errors.add(path, "mcp_json.mcpServers must be a non-empty object")
        return
    if name in servers:
        server = servers[name]
    elif len(servers) == 1:
        server = next(iter(servers.values()))
    else:
        errors.add(path, "mcp_json: ship exactly one server (or name it after the plugin)")
        return
    if not isinstance(server, dict):
        errors.add(path, "mcp_json: server must be an object")
        return
    server_type = server.get("type")
    if server_type == "streamable-http":
        url = server.get("url")
        if not isinstance(url, str) or not url.lower().startswith("https://"):
            errors.add(path, f"mcp_json: url must be https:// (got {url!r})")
        if server.get("headers"):
            errors.add(
                path,
                "mcp_json: headers are not allowed (token injection is a client concern)",
            )
    elif server_type == "stdio":
        command = server.get("command", "")
        launcher = str(command).replace("\\", "/").rsplit("/", 1)[-1].lower()
        launcher = launcher.removesuffix(".exe").removesuffix(".cmd")
        if launcher not in STDIO_LAUNCHERS:
            errors.add(path, f"mcp_json: launcher {command!r} not in allowlist {STDIO_LAUNCHERS}")
        args = server.get("args", [])
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            errors.add(path, "mcp_json: args must be strings")
            args = []
        for arg in args:
            if arg.endswith("@latest"):
                errors.add(path, f"mcp_json: {arg!r} is unpinned — pin an exact version")
        env = server.get("env", {})
        if not isinstance(env, dict):
            errors.add(path, "mcp_json: env must be an object")
            env = {}
        for key, value in env.items():
            if not isinstance(value, str) or not ENV_PLACEHOLDER_RE.fullmatch(value):
                errors.add(path, f"mcp_json: env {key!r} must be a $plugin_… placeholder")
    elif server_type == "sse":
        errors.add(path, "mcp_json: deprecated 'sse' transport — publish streamable-http")
    else:
        errors.add(path, f"mcp_json: unknown server type {server_type!r}")


def validate_plugin(doc: dict, name: str, errors: Errors, path: Path) -> None:
    plugin_json = doc.get("plugin_json")
    if not isinstance(plugin_json, dict):
        errors.add(path, "plugin_json (object) is required for kind=plugin")
        return
    if plugin_json.get("$schema") != PLUGIN_SCHEMA:
        errors.add(path, f"plugin_json.$schema must be {PLUGIN_SCHEMA}")
    if plugin_json.get("name") != name:
        errors.add(path, "plugin_json.name must equal the submission name")
    description = plugin_json.get("description")
    if not isinstance(description, str) or not description.strip():
        errors.add(path, "plugin_json.description is required")
    elif len(description) > MAX_DESCRIPTION_CHARS:
        errors.add(path, f"plugin_json.description longer than {MAX_DESCRIPTION_CHARS} chars")
    extensions = plugin_json.get("extensions")
    extension = extensions.get(EXTENSION_NAMESPACE) if isinstance(extensions, dict) else None
    if not isinstance(extension, dict):
        errors.add(path, f'plugin_json.extensions["{EXTENSION_NAMESPACE}"] is required')
        return
    if extension.get("native_tool"):
        errors.add(path, "native_tool is repo-contributed code — not publishable here")
    auth = extension.get("auth")
    if not isinstance(auth, dict) or auth.get("mode") not in AUTH_MODES:
        errors.add(path, f"extension auth.mode must be one of {AUTH_MODES}")
    template = extension.get("mcp_auth_header_template")
    if template is not None:
        if not isinstance(template, str) or not HEADER_PLACEHOLDER_RE.search(template):
            errors.add(path, "mcp_auth_header_template must use a $plugin_… placeholder")
        elif TOKEN_LITERAL_RE.search(HEADER_PLACEHOLDER_RE.sub(" ", template)):
            errors.add(path, "mcp_auth_header_template may not embed literal credentials")
    mcp_json = doc.get("mcp_json")
    if mcp_json is not None:
        if not isinstance(mcp_json, dict):
            errors.add(path, "mcp_json must be an object or null")
        else:
            validate_mcp_json(name, mcp_json, errors, path)
    usage_card = doc.get("usage_card")
    if usage_card is not None and not isinstance(usage_card, str):
        errors.add(path, "usage_card must be a string or null")
    reject_http_urls(doc, "submission", errors, path)


def validate_skill(doc: dict, name: str, errors: Errors, path: Path) -> None:
    skill_md = doc.get("skill_md")
    if not isinstance(skill_md, str) or not skill_md.strip():
        errors.add(path, "skill_md (the full SKILL.md text) is required for kind=skill")
        return
    if len(skill_md.encode("utf-8")) > MAX_SKILL_BYTES:
        errors.add(path, f"skill_md larger than {MAX_SKILL_BYTES} bytes")
    if not skill_md.lstrip().startswith("---"):
        errors.add(path, "skill_md must start with YAML frontmatter (---)")
    if f"name: {name}" not in skill_md:
        errors.add(path, "skill_md frontmatter must declare the same name as the submission")
    title = doc.get("title")
    if not isinstance(title, str) or not title.strip():
        errors.add(path, "title is required for kind=skill")
    description = doc.get("description")
    if not isinstance(description, str) or not description.strip():
        errors.add(path, "description is required for kind=skill")
    elif len(description) > MAX_DESCRIPTION_CHARS:
        errors.add(path, f"description longer than {MAX_DESCRIPTION_CHARS} chars")
    categories = doc.get("categories", [])
    if not isinstance(categories, list) or not all(isinstance(c, str) for c in categories):
        errors.add(path, "categories must be a list of strings")


def read_base_version(path: Path, base_ref: str) -> dict | None:
    """The submission as it exists at base_ref, or None when new."""
    rel = path.resolve().relative_to(ROOT).as_posix()
    proc = subprocess.run(
        ["git", "show", f"{base_ref}:{rel}"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return None


def version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def account_id(doc: dict) -> int | None:
    """The submission's numeric GitHub account id, or None when absent/invalid.

    ``bool`` is a subclass of ``int`` in Python, so ``true`` would otherwise
    pass as the id ``1``.
    """
    value = doc.get("publisher_id")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def validate_file(path: Path, errors: Errors, base_ref: str | None) -> None:
    raw = path.read_bytes()
    if len(raw) > MAX_FILE_BYTES:
        errors.add(path, f"file larger than {MAX_FILE_BYTES} bytes")
        return
    text = raw.decode("utf-8", errors="replace")
    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            errors.add(
                path, f"matches credential pattern {pattern.pattern!r} — never submit secrets"
            )
    try:
        doc = json.loads(text)
    except ValueError as exc:
        errors.add(path, f"not valid JSON: {exc}")
        return
    if not isinstance(doc, dict):
        errors.add(path, "submission must be a JSON object")
        return

    name = doc.get("name")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name) or "--" in name or ".." in name:
        errors.add(path, f"name {name!r} violates the naming rules (a-z 0-9 - . / no '--' '..')")
        return
    if path.name != f"{name}.json":
        errors.add(path, f"file must be named {name}.json")
    kind = doc.get("kind")
    if kind not in ("plugin", "skill"):
        errors.add(path, "kind must be 'plugin' or 'skill'")
        return
    if kind == "plugin" and name in RESERVED_PLUGIN_IDS:
        errors.add(path, f"{name!r} is a built-in Personal Jarvis plugin id — reserved")
    if kind == "skill" and name in RESERVED_SKILL_NAMES:
        errors.add(path, f"{name!r} is a built-in Personal Jarvis skill name — reserved")

    publisher = doc.get("publisher")
    if not isinstance(publisher, str) or not GITHUB_LOGIN_RE.fullmatch(publisher):
        errors.add(path, "publisher must be the author's GitHub username")
    publisher_id = account_id(doc)
    if doc.get("publisher_id") is not None and publisher_id is None:
        errors.add(path, "publisher_id must be a positive integer (your numeric GitHub account id)")
    version = doc.get("version")
    if not isinstance(version, str) or not SEMVER_RE.fullmatch(version):
        errors.add(path, "version must be SemVer (MAJOR.MINOR.PATCH)")

    if kind == "plugin":
        validate_plugin(doc, name, errors, path)
    else:
        validate_skill(doc, name, errors, path)

    if base_ref and isinstance(version, str) and SEMVER_RE.fullmatch(version):
        base = read_base_version(path, base_ref)
        if base is not None:
            base_id = account_id(base)
            if base_id is not None:
                # Once an entry records an id, that id is the ONLY ownership
                # key. Dropping the field must not fall back to the login
                # comparison — omitting it would otherwise be the bypass.
                if publisher_id is None:
                    errors.add(
                        path,
                        "publisher_id is required to update this entry — it is owned by "
                        f"account id {base_id}. Add your numeric GitHub account id.",
                    )
                elif publisher_id != base_id:
                    errors.add(
                        path,
                        f"publisher_id may not change on an update (ownership rule): "
                        f"this entry belongs to account id {base_id}",
                    )
            elif base.get("publisher") != publisher:
                errors.add(path, "publisher may not change on an update (ownership rule)")
            base_version = str(base.get("version", "0.0.0"))
            base_ok = SEMVER_RE.fullmatch(base_version)
            if base_ok and version_tuple(version) <= version_tuple(base_version):
                errors.add(path, f"version must increase (base has {base_version})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*", help="submission files (default: submissions/*.json)")
    parser.add_argument("--base-ref", default=None, help="git ref for ownership/version checks")
    args = parser.parse_args()

    files = [Path(f) for f in args.files] or sorted((ROOT / "submissions").glob("*.json"))
    errors = Errors()
    for path in files:
        if not path.exists():
            errors.add(path, "file does not exist")
            continue
        validate_file(path, errors, args.base_ref)

    if errors.items:
        print(f"FAIL — {len(errors.items)} problem(s):")
        for item in errors.items:
            print(f"  - {item}")
        return 1
    print(f"OK — {len(files)} submission(s) valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
