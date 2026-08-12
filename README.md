# Personal Jarvis Marketplace

The community registry for [Personal Jarvis](https://github.com/PersonalJarvis/PersonalJarvis)
plugins and skills. Anyone can publish; every submission that passes the
automated checks is listed — there is no human review queue. Users see a
"Community · not reviewed" badge and an explicit consent dialog in the app
before anything is installed.

- **Browse:** in the app under **Plugins → Community**, or on the
  storefront at <https://personaljarvis.ai/marketplace>.
- **Publish:** use the form at <https://personaljarvis.ai/marketplace/submit>
  — it builds your submission file and opens a pull request here for you
  (a GitHub account is your publisher identity).
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
   the PR changes exactly that one file and that the `publisher` field
   equals the PR author, then merges. Everything else waits for a
   maintainer.
4. On main, the bot expands the submission into an
   [Agent Plugins v1.0.0](https://agent-plugins.org/) package under
   `plugins/` (or `skills/<name>/SKILL.md`), records ownership in
   `registry.json`, compiles `index.json`, and deploys it to Pages.

## Ownership and updates

The first merged submission of a name claims it: `registry.json` records
your GitHub login as the publisher. Updates auto-merge only from the same
account and must increase `version`. Name changes are new submissions.

## Submission format

One JSON file per item — see [`schemas/submission.schema.json`](schemas/submission.schema.json)
and the live examples in [`submissions/`](submissions/).

**Plugin** (`kind: "plugin"`): an embedded Agent Plugins v1.0.0
`plugin_json` (Jarvis specifics under the `io.github.personaljarvis`
extension: auth mode, branding, category), an optional `mcp_json`
(one `streamable-http` or pinned `stdio` server), and an optional
`usage_card` (keywords that help Jarvis offer the plugin on relevant turns).

**Skill** (`kind: "skill"`): `title`, `description`, `categories`, and the
full `skill_md` (a `SKILL.md` with YAML frontmatter — the app validates it
on install and shows it to the user before it can run).

## Trust model, stated plainly

Nothing here is reviewed by a human before it is listed. The automated
checks stop credential smuggling, plaintext endpoints, unpinned code
execution, and name hijacking — they cannot judge whether a service is
trustworthy. The app therefore shows every community entry unbadged as
unreviewed and displays the exact endpoint or command before installing.
Report a malicious listing by opening an issue; a revert delists it within
minutes.
