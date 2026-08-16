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
MAX_BUNDLED_SKILLS = 10
MAX_DESCRIPTION_CHARS = 500
MAX_WALLPAPER_TITLE_CHARS = 80
# Ceiling for the image beside a wallpaper submission. A 4K WebP at gallery
# quality is well under 2 MB; 8 MB refuses a mis-committed original without
# refusing any legitimate wallpaper.
MAX_WALLPAPER_BYTES = 8 * 1024 * 1024

# Licenses a community wallpaper may carry. Redistribution is the whole
# point of the lane — the feed serves the file to every install — so only
# licenses that permit exactly that are accepted.
WALLPAPER_LICENSES = ("CC0-1.0", "CC-BY-4.0", "CC-BY-SA-4.0")

# Agent Plugins v1.0.0 name rules.
NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,62}[a-z0-9])?$")
GITHUB_LOGIN_RE = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9]|-(?=[a-zA-Z0-9])){0,38}$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
ENV_PLACEHOLDER_RE = re.compile(r"^\$(?:plugin_[a-z0-9_.-]+|\{plugin_[a-z0-9_.-]+\})$")
HEADER_PLACEHOLDER_RE = re.compile(r"\$(?:plugin_[a-z0-9_.-]+|\{plugin_[a-z0-9_.-]+\})")
TOKEN_LITERAL_RE = re.compile(r"[A-Za-z0-9_\-]{20,}")

STDIO_LAUNCHERS = ("npx", "uvx", "docker")

# The launcher name alone decides nothing: every one of the three allowed
# launchers has options that turn it back into "run whatever I say".
# `npx -p x@1.0.0 -c "curl … | sh"` runs a shell one-liner, `uvx --with evil`
# installs a second package nobody reviewed, and `docker run -v /:/host`
# hands the container the user's whole disk. So the ARGUMENTS are an
# allowlist too, per launcher, and the package a launcher fetches must be a
# pinned name from its own registry — never a git ref, a URL, or a path.
#
# The rule is positional: everything up to and including the package
# specification belongs to the LAUNCHER and is checked; everything after it
# is passed to the server the publisher wrote anyway, so checking it would
# buy nothing.
NPX_FLAGS = frozenset({"-y", "--yes"})
UVX_FLAGS = frozenset({"--from"})  # takes a value, itself pinned
DOCKER_FLAGS = frozenset({"-i", "--interactive", "--rm"})
DOCKER_VALUE_FLAGS = frozenset({"-e", "--env"})  # NAME only — see below

# npm: `pkg@1.2.3` or `@scope/pkg@1.2.3`. No `github:`, no `git+https://`,
# no `file:`, no tarball URL — those are not registry packages and carry no
# version anyone can pin.
NPM_PIN_RE = re.compile(
    r"^(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*@\d+\.\d+\.\d+[0-9A-Za-z.+-]*$"
)
# PyPI: `pkg==1.2.3` (uv's own spelling) or `pkg@1.2.3`.
PYPI_PIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:==|@)\d+\.\d+\.\d+[0-9A-Za-z.+-]*$")
# A container image with an explicit version tag or a digest. `:latest` and a
# bare name are both "whatever the publisher pushes next".
IMAGE_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*:[0-9][0-9A-Za-z._-]*$")
IMAGE_DIGEST_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$")
# Only a bare variable NAME may be handed to `docker -e`. `-e PATH=/evil`
# would rewrite the container's environment from the manifest; the values a
# plugin legitimately needs travel through the env block, which is checked.
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Variables that decide what runs, before the plugin's own first line does.
# A manifest configures its own server; it does not get to re-point the
# dynamic linker, the interpreter, or the PATH the launcher is resolved on.
RESERVED_ENV_NAMES = frozenset(
    {
        "PATH",
        "PATHEXT",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "LD_AUDIT",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONHOME",
        "NODE_OPTIONS",
        "NODE_PATH",
        "NPM_CONFIG_REGISTRY",
        "UV_INDEX_URL",
        "UV_EXTRA_INDEX_URL",
        "PIP_INDEX_URL",
        "PIP_EXTRA_INDEX_URL",
        "DOCKER_HOST",
        "COMSPEC",
        "SYSTEMROOT",
        "WINDIR",
    }
)

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


def validate_stdio_args(launcher: str, args: list[str]) -> str | None:
    """Check the launcher half of an stdio argv against its own allowlist.

    Returns the first problem as a sentence naming the offending argument, or
    ``None`` when the argv is acceptable. The message ends up in the pull
    request check and in the app's install dialog, so it says what to change
    rather than that something was wrong.

    Mirrors ``_validate_stdio_args`` in the app's agent_plugins_loader.py and
    ``validateStdioArgs`` in the storefront's functions/_lib/validate.ts.
    Change one, change all three.
    """
    if launcher == "npx":
        index = 0
        while index < len(args) and args[index].startswith("-"):
            if args[index] not in NPX_FLAGS:
                return (
                    f"npx option {args[index]!r} is not allowed — a community "
                    "package may only be launched as `npx -y <name>@<version>` "
                    "(options like -p/-c can run arbitrary commands)"
                )
            index += 1
        if index >= len(args):
            return "npx needs a package to run, e.g. `npx -y my-mcp@1.2.0`"
        if not NPM_PIN_RE.fullmatch(args[index]):
            return (
                f"{args[index]!r} is not a pinned npm package — community stdio "
                "packages must name a registry package with an exact version "
                "(`my-mcp@1.2.0`), never a git ref, URL or path"
            )
        return None

    if launcher == "uvx":
        index = 0
        while index < len(args) and args[index].startswith("-"):
            flag, sep, inline = args[index].partition("=")
            if flag not in UVX_FLAGS:
                return (
                    f"uvx option {flag!r} is not allowed — a community package "
                    "may only be launched as `uvx <name>==<version>` "
                    "(options like --with install code nobody reviewed)"
                )
            if sep:
                value, index = inline, index + 1
            else:
                if index + 1 >= len(args):
                    return f"uvx option {flag!r} is missing its value"
                value, index = args[index + 1], index + 2
            if not PYPI_PIN_RE.fullmatch(value):
                return (
                    f"{value!r} is not a pinned PyPI package — use "
                    "`--from my-mcp==1.2.0`, never a git ref, URL or path"
                )
            # `--from` names the package; what follows is the entry point.
            return None
        if index >= len(args):
            return "uvx needs a package to run, e.g. `uvx my-mcp==1.2.0`"
        if not PYPI_PIN_RE.fullmatch(args[index]):
            return (
                f"{args[index]!r} is not a pinned PyPI package — community stdio "
                "packages must name a package with an exact version "
                "(`my-mcp==1.2.0`), never a git ref, URL or path"
            )
        return None

    # docker
    if not args or args[0] != "run":
        return "the only allowed docker subcommand is `run`"
    index = 1
    while index < len(args) and args[index].startswith("-"):
        flag, sep, inline = args[index].partition("=")
        if flag in DOCKER_VALUE_FLAGS:
            if sep:
                value, index = inline, index + 1
            else:
                if index + 1 >= len(args):
                    return f"docker option {flag!r} is missing its value"
                value, index = args[index + 1], index + 2
            if not ENV_NAME_RE.fullmatch(value):
                return (
                    f"docker {flag} {value!r} must name a variable and nothing "
                    "else — a plugin's own values belong in the env block, "
                    "which is checked for smuggled credentials"
                )
            continue
        if flag not in DOCKER_FLAGS:
            return (
                f"docker option {flag!r} is not allowed — a community container "
                "runs as `docker run -i --rm <image>:<version>`; options like "
                "-v/--mount/--privileged/--network would open the user's machine "
                "to it"
            )
        index += 1
    if index >= len(args):
        return "docker needs an image to run, e.g. `docker run -i --rm my/mcp:1.2`"
    image = args[index]
    if not (IMAGE_TAG_RE.fullmatch(image) or IMAGE_DIGEST_RE.fullmatch(image)):
        return (
            f"{image!r} is not a pinned image — name an explicit version tag "
            "(`my/mcp:1.2`) or a digest, never `latest` or a bare name"
        )
    return None


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
            launcher = ""
        if server.get("url"):
            # One server entry, one transport. A stdio entry that also carries
            # a url reads as a hosted server to anything that checks the url
            # first and runs the command anyway — the two halves must never
            # disagree about what this entry is.
            errors.add(
                path,
                "mcp_json: a stdio server may not also declare a url — "
                "publish either a local command or a hosted endpoint",
            )
        args = server.get("args", [])
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            errors.add(path, "mcp_json: args must be strings")
            args = []
        if launcher:
            problem = validate_stdio_args(launcher, list(args))
            if problem:
                errors.add(path, f"mcp_json: {problem}")
        env = server.get("env", {})
        if not isinstance(env, dict):
            errors.add(path, "mcp_json: env must be an object")
            env = {}
        for key, value in env.items():
            name_text = str(key)
            if not ENV_NAME_RE.fullmatch(name_text) or name_text.upper() in RESERVED_ENV_NAMES:
                # A manifest sets variables for ITS server, never for the
                # machinery that starts it: PATH decides which binary `npx`
                # even is, and the loader hooks run code before the server's
                # first line does.
                errors.add(
                    path,
                    f"mcp_json: env {name_text!r} is not a plugin variable — "
                    "PATH and the loader/runtime hooks are off limits",
                )
            if not isinstance(value, str) or not ENV_PLACEHOLDER_RE.fullmatch(value):
                errors.add(path, f"mcp_json: env {key!r} must be a $plugin_… placeholder")
    elif server_type == "sse":
        errors.add(path, "mcp_json: deprecated 'sse' transport — publish streamable-http")
    else:
        errors.add(path, f"mcp_json: unknown server type {server_type!r}")


# Top-level frontmatter keys a marketplace skill may not declare.
#
# risk_policy is a privilege boundary: the app evaluates a skill's tools
# against the SKILL'S declared tier rather than the tool's own, so an
# auto-merged author could mark a dangerous tool "safe" and skip the
# confirmation it was given. Mirrors
# jarvis/marketplace/agent_plugins_loader.py — a rule enforced on only one
# of the two publishing routes is a rule authors route around.
FORBIDDEN_SKILL_KEYS = ("risk_policy",)
TOP_LEVEL_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:", re.MULTILINE)

# Executable payloads inside a skill. The standard allows scripts/; community
# packages ship instructions, and the index embeds SKILL.md only.
FORBIDDEN_SKILL_DIRS = ("scripts",)


def frontmatter_block(skill_md: str) -> str:
    text = skill_md.lstrip()
    if not text.startswith("---"):
        return ""
    _, _, rest = text.partition("---")
    block, sep, _ = rest.partition("\n---")
    return block if sep else ""


def validate_skill_document(
    skill_md: object, name: str, errors: Errors, path: Path, where: str
) -> None:
    """Every rule a SKILL.md must satisfy, standalone or bundled."""
    if not isinstance(skill_md, str) or not skill_md.strip():
        errors.add(path, f"{where}: the full SKILL.md text is required")
        return
    if len(skill_md.encode("utf-8")) > MAX_SKILL_BYTES:
        errors.add(path, f"{where}: larger than {MAX_SKILL_BYTES} bytes")
    block = frontmatter_block(skill_md)
    if not block.strip():
        errors.add(path, f"{where}: must start with YAML frontmatter (---)")
        return
    declared = {m.group(1) for m in TOP_LEVEL_KEY_RE.finditer(block)}
    for key in FORBIDDEN_SKILL_KEYS:
        if key in declared:
            errors.add(
                path,
                f"{where}: {key!r} may not be declared — it governs which "
                "tools run without confirmation; the built-in default applies",
            )
    for required in ("name", "description"):
        if required not in declared:
            errors.add(path, f"{where}: frontmatter is missing {required!r}")
    if f"name: {name}" not in block:
        errors.add(path, f"{where}: frontmatter name must equal {name!r}")


def validate_bundled_skills(doc: dict, name: str, errors: Errors, path: Path) -> int:
    """The optional skills block on a plugin submission. Returns the count."""
    raw = doc.get("skills")
    if raw is None:
        return 0
    if not isinstance(raw, list):
        errors.add(path, "skills must be a list")
        return 0
    if len(raw) > MAX_BUNDLED_SKILLS:
        errors.add(path, f"at most {MAX_BUNDLED_SKILLS} bundled skills are accepted")
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            errors.add(path, "each bundled skill must be an object")
            continue
        skill_name = item.get("name")
        if not isinstance(skill_name, str) or not NAME_RE.fullmatch(skill_name):
            errors.add(path, f"bundled skill name {skill_name!r} violates the name rules")
            continue
        if "--" in skill_name or ".." in skill_name:
            errors.add(path, f"bundled skill name {skill_name!r} violates the name rules")
            continue
        if skill_name in seen:
            errors.add(path, f"bundled skill {skill_name!r} appears twice")
            continue
        seen.add(skill_name)
        for key in item:
            if key in FORBIDDEN_SKILL_DIRS:
                errors.add(path, f"bundled skill {skill_name!r}: {key}/ may not be published")
        validate_skill_document(
            item.get("skill_md"), skill_name, errors, path, f"skills[{skill_name}]"
        )
    return len(seen)


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
    skill_count = validate_bundled_skills(doc, name, errors, path)
    # A package must carry something that works. Native bindings are rejected
    # above, so without an MCP server, a hosted MCP auth URL, or skills, the
    # store would show a card that collects a token and offers nothing.
    has_auth_mcp = isinstance(auth, dict) and bool(auth.get("mcp_url"))
    if mcp_json is None and not has_auth_mcp and skill_count == 0:
        errors.add(
            path,
            "package has no components — needs an mcp_json, a hosted MCP auth "
            "mode, or bundled skills",
        )
    reject_http_urls(doc, "submission", errors, path)


# The containers a browser can actually produce: not every engine encodes
# WebP, so an upload may deliver JPEG or PNG. The published SITE still serves
# WebP only — the publish build re-encodes whatever arrived here.
WALLPAPER_MAGIC = {
    "wallpaper.webp": (b"RIFF", b"WEBP"),
    "wallpaper.jpg": (b"\xff\xd8\xff", None),
    "wallpaper.png": (b"\x89PNG", None),
}


def wallpaper_image_paths(name: str) -> list[Path]:
    """The image files present for a wallpaper submission (should be one)."""
    folder = ROOT / "wallpapers" / name
    return [folder / filename for filename in WALLPAPER_MAGIC if (folder / filename).is_file()]


def validate_wallpaper(doc: dict, name: str, errors: Errors, path: Path) -> None:
    """kind=wallpaper: metadata rules plus the image file beside it.

    Nobody looks at the picture before it publishes, and no pattern list can
    recognize a hateful or illegal one — so what CI settles here is strictly
    everything a machine CAN settle: the metadata shape and that the
    committed file is a plausibly-sized image. Judging the depiction is left
    to reports after the fact, which the storefront's delist path answers.

    The publish build re-encodes the image (whatever its container) before it
    reaches the site, so the served bytes are always freshly produced, never
    the committed file.
    """
    title = doc.get("title")
    if not isinstance(title, str) or not title.strip():
        errors.add(path, "title is required for kind=wallpaper")
    elif len(title) > MAX_WALLPAPER_TITLE_CHARS:
        errors.add(path, f"title longer than {MAX_WALLPAPER_TITLE_CHARS} chars")
    description = doc.get("description")
    if description is not None:
        if not isinstance(description, str):
            errors.add(path, "description must be a string")
        elif len(description) > MAX_DESCRIPTION_CHARS:
            errors.add(path, f"description longer than {MAX_DESCRIPTION_CHARS} chars")
    license_id = doc.get("license")
    if license_id not in WALLPAPER_LICENSES:
        errors.add(
            path,
            f"license must be one of {WALLPAPER_LICENSES} — the feed "
            "redistributes the file, so the license must allow that",
        )
    theme = doc.get("theme")
    if theme is not None and theme not in ("light", "dark"):
        errors.add(path, "theme must be 'light', 'dark', or omitted")

    images = wallpaper_image_paths(name)
    if not images:
        errors.add(
            path,
            f"wallpapers/{name}/wallpaper.(webp|jpg|png) is missing — the "
            "upload form commits the image beside this submission",
        )
    elif len(images) > 1:
        errors.add(path, "exactly one image file may exist per wallpaper")
    else:
        image = images[0]
        size = image.stat().st_size
        if size > MAX_WALLPAPER_BYTES:
            errors.add(path, f"image is {size} bytes — larger than {MAX_WALLPAPER_BYTES}")
        with image.open("rb") as handle:
            header = handle.read(12)
        prefix, riff_tag = WALLPAPER_MAGIC[image.name]
        magic_ok = header.startswith(prefix) and (riff_tag is None or header[8:12] == riff_tag)
        if not magic_ok:
            errors.add(path, f"{image.name} does not carry its container's magic bytes")

    reject_http_urls(doc, "submission", errors, path)


def validate_skill(doc: dict, name: str, errors: Errors, path: Path) -> None:
    validate_skill_document(doc.get("skill_md"), name, errors, path, "skill_md")
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
    validate_flavor(doc, errors, path)


# Which frontmatter a skill declares. Optional and free to omit: build_index.py
# derives it from the file itself, so an author who never heard of the field
# still gets the right mark. Stating it is for the case where the derivation
# would be wrong — a Jarvis-flavored file whose author also maintains it for
# other agents, say. Mirrors SKILL_FLAVORS in
# jarvis/marketplace/community_source.py and build_index.py.
SKILL_FLAVORS = ("jarvis", "portable")

# The publisher's "also runs in" list. Display-only text that lands in the
# store UI, so it is bounded HERE too: the feed's own cleanup would silently
# truncate, and a submitter deserves to be told instead.
MAX_COMPATIBLE_AGENTS = 8
MAX_AGENT_NAME_CHARS = 32


def validate_flavor(doc: dict, errors: Errors, path: Path) -> None:
    """The optional portability fields on a skill submission."""
    flavor = doc.get("flavor")
    if flavor is not None and (
        not isinstance(flavor, str) or flavor not in SKILL_FLAVORS
    ):
        errors.add(path, f"flavor must be one of {list(SKILL_FLAVORS)} when set")

    agents = doc.get("compatible_agents")
    if agents is None:
        return
    if not isinstance(agents, list) or not all(isinstance(a, str) for a in agents):
        errors.add(path, "compatible_agents must be a list of strings")
        return
    if len(agents) > MAX_COMPATIBLE_AGENTS:
        errors.add(path, f"compatible_agents: at most {MAX_COMPATIBLE_AGENTS} entries")
    for agent in agents:
        if len(agent) > MAX_AGENT_NAME_CHARS:
            errors.add(
                path,
                f"compatible_agents: {agent[:20]!r}… longer than "
                f"{MAX_AGENT_NAME_CHARS} chars",
            )


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
    if kind not in ("plugin", "skill", "wallpaper"):
        errors.add(path, "kind must be 'plugin', 'skill' or 'wallpaper'")
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
    elif kind == "wallpaper":
        validate_wallpaper(doc, name, errors, path)
    else:
        validate_skill(doc, name, errors, path)

    if base_ref and isinstance(version, str) and SEMVER_RE.fullmatch(version):
        base = read_base_version(path, base_ref)
        if base == doc:
            # The workflow validates EVERY submission, not only the ones a
            # PR touches. A file identical to its base version is untouched
            # in this change — the update rules (ownership, version bump)
            # must not fire for it, or every PR fails on the back catalog.
            base = None
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
