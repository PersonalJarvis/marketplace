# Personal Jarvis Marketplace

The community registry for [Personal Jarvis](https://github.com/PersonalJarvis/PersonalJarvis)
plugins and skills. Anyone can publish; every submission that passes the
automated checks is listed — there is no human review queue. Users see a
"Community · not reviewed" badge and an explicit consent dialog in the app
before anything is installed.

- **Browse:** in the app under **Plugins → Community**. A storefront browse
  page is in build.
- **Publish:** open a pull request that adds one `submissions/<name>.json`
  (see [Submission format](#submission-format)). A sign-in-with-GitHub
  upload form that builds the file for you is in build — until it ships,
  the pull request is the way in.
- **Feed:** the compiled [`index.json`](https://personaljarvis.github.io/marketplace/index.json)
  is what the app and the storefront read.

## How publishing works

```
submissions/<name>.json  ──PR──►  automated checks  ──green──►  auto-merge
                                                                    │
plugins/<name>/…  skills/<name>/SKILL.md  registry.json  ◄── expansion (bot)
                                                                    │
                        GitHub Pages: index.json  ◄── compile + deploy
```

1. A pull request adds or updates **one** file: `submissions/<name>.json`.
2. `validate` checks it: naming rules, reserved names, https-only URLs,
   stdio launcher allowlist with pinned versions, no credentials anywhere,
   size limits (see `scripts/validate.py` — the app re-enforces the same
   rules at install time).
3. The `automerge` gate (trusted code, never executes PR content) verifies
   the PR changes exactly that one file and that the publisher is proven —
   either because you opened the pull request yourself (`publisher` and
   `publisher_id` must be yours), or because the submission came through
   the upload form, whose GitHub App pushed the branch into this repo after
   verifying your sign-in. Then it merges. Everything else waits for a
   maintainer.
4. On main, the bot expands the submission into an
   [Agent Plugins v1.0.0](https://agent-plugins.org/) package under
   `plugins/` (or `skills/<name>/SKILL.md`), records ownership in
   `registry.json`, compiles `index.json`, and deploys it to Pages.

## Ownership and updates

The first merged submission of a name claims it. `registry.json` records
two things about the publisher: `publisher_id`, your **numeric GitHub
account id**, and `publisher`, your login — shown to humans, never used to
decide anything.

That split is deliberate. A GitHub login can be renamed, and the freed name
can then be registered by a stranger; if ownership hung on the login string,
that stranger would inherit every entry published under it. Your account id
never changes, so it is what the gate compares.

Updates auto-merge only from the account that holds the entry and must
increase `version`. Once an entry records a `publisher_id`, every later
update must carry the same one — leaving the field out is rejected, not
waved through. Entries published before the field existed still compare
logins until their next update. Name changes are new submissions.

Find your account id at `https://api.github.com/users/<your-login>` — the
submit form fills it in for you.

## Submission format

One JSON file per item — see [`schemas/submission.schema.json`](schemas/submission.schema.json)
and the live examples in [`submissions/`](submissions/).

The limits, patterns, reserved names and allowlists are generated from the
validator into [`rules.json`](rules.json) and published at
<https://personaljarvis.github.io/marketplace/rules.json>. Anything that
checks a submission before it reaches CI — the upload form, the app — reads
that file instead of retyping the values, and CI fails if the two drift
apart. `scripts/validate.py` stays the authority for the rules that are
logic rather than data.

**Plugin** (`kind: "plugin"`): an embedded Agent Plugins v1.0.0
`plugin_json` (Jarvis specifics under the `io.github.personaljarvis`
extension: auth mode, branding, category), an optional `mcp_json`
(one `streamable-http` or pinned `stdio` server), an optional `skills`
array, and an optional `usage_card` (keywords that help Jarvis offer the
plugin on relevant turns). A package must carry at least one working
component — an entry that only collects a token is refused.

**Skill** (`kind: "skill"`): `title`, `description`, `categories`, and the
full `skill_md` (a `SKILL.md` with YAML frontmatter — the app validates it
on install and shows it to the user before it can run).

### Bundling skills with a plugin

A plugin may ship the instructions for using it, which is what the Agent
Plugins standard is for: tools and guidance installed together. Add them to
the submission as

```json
"skills": [{ "name": "sentry-triage", "skill_md": "---\nname: sentry-triage\n..." }]
```

and the expansion writes a real `plugins/<name>/skills/<skill>/SKILL.md`, so
the published directory is a package any client implementing the standard
can read. Installing the plugin writes those skills into the user's skills
folder — named on the consent dialog beforehand — and removing the plugin
takes them away again.

Two limits, enforced by CI and again by the app:

* **No `scripts/`.** The standard allows a skill to ship executables; this
  registry publishes instructions and reference text only.
* **No `risk_policy` in the frontmatter.** That field decides which tools run
  without asking the user, and nothing here is reviewed by a human — the
  built-in default applies instead.

## Wallpapers — the one reviewed lane

Wallpapers work differently from everything above, in exactly one way:
**they never auto-merge.** No pattern list can recognize a hateful or
illegal image, so a maintainer looks at every picture before it is
published. Submissions arrive through the storefront's upload form, wait in
a private review queue, and only an approval commits the pair
`submissions/<name>.json` + `wallpapers/<name>/wallpaper.webp` here
(`kind: "wallpaper"`, `title`, a redistribution license out of CC0-1.0 /
CC-BY-4.0 / CC-BY-SA-4.0, optional `theme`). A wallpaper pull request
opened directly stays open for that same review (`automerge_gate.py`).

The publish build re-encodes every image and derives a grid thumbnail
(`build_index.py`), so the site serves freshly-produced bytes — EXIF,
appended payloads and forged headers do not survive — from
`…/wallpapers/<name>/wallpaper.webp` and `…/thumb.webp` on this Pages site.
The app imports a wallpaper with one click (or
`jarvis marketplace install <name>`); it lands in the wallpaper picker
under "Yours".

## Trust model, stated plainly

Nothing here except wallpapers is reviewed by a human before it is listed.
The automated checks stop credential smuggling, plaintext endpoints,
unpinned code execution, and name hijacking — they cannot judge whether a
service is trustworthy. The app therefore shows every community plugin and
skill as unreviewed and displays the exact endpoint or command before
installing. Wallpapers are the inverse: every image is seen by a maintainer
before publication. Report a malicious listing by opening an issue; a
revert delists it within minutes.
