# Jira transitions and Slack notice: roadmap

## Goal
After a release, the Jira tickets it carries move to the right status of each project's workflow (after a review
checklist), and the team gets a Slack message with what shipped. Both are built-in connectors of release-cli.

## Decisions
- Built-ins in release-cli (`jira`, `slack-notify`), like `cursor-review` and `github-release` — they update with
  `release update`. The repo is public: the code is generic; URLs, keys, workflows and channels live in config.
- Tickets come from the commits between the previous tag and this one (same range as `release notes`), shown as a
  checklist with every ticket of the project's key ticked; the user unticks or adds keys before anything runs.
  This needs a new protocol question type `multi` (backward compatible addition to protocol 1).
- Jira: only a status transition, no fixVersion. Each project's workflow differs, so the first run in a project
  reads the real transitions from Jira and asks which one applies on an RC and which on a final; saved in `.release`.
  A ticket where that transition is not available (another state) is skipped and reported, never forced.
- Slack via incoming webhook only (no bot token). Teams is out.
- Secrets (Jira API token, Slack webhook URL) never go into TOML, run state, `prior`, or logs: env var first, then
  the OS keychain, set with `release secret set <name>`.
- `slack-notify` notifies finals by default (`notify_on = "final"`); `every-deploy` is the per-project opt-in.
- Jira Cloud only (REST v3, email + API token). Data Center is out.
- Ticket keys of another project found in the commits are listed unticked, not hidden.
- Questions are asked before any connector runs (CLI rule); transitions and the notice run after the deploy
  connector reports `SUCCESS` when one is in the chain, otherwise right after the push.

## Contracts
**`multi` question** (CLI side, T-01):
`{"id": "issues", "type": "multi", "prompt": "...", "options": [{"value": "ABC-1", "label": "ABC-1 Fix login (In Progress)"}], "default": ["ABC-1"], "other": {"pattern": "[A-Z][A-Z0-9_]+-[0-9]+"}}`
- `options` may also be plain strings; `default` is a subset of the values; `other` (optional) allows extra values
  matching `pattern`. Answer: a list of strings (option order, then extras). `persist` must be `none`.
- Terminal: numbered list with `[x]`/`[ ]`; Enter accepts; `2 5` toggles; `+KEY` adds, `-KEY` removes.
  `--defaults` takes `default`. `--dry-run` lists the options and the default.

**Secrets** (T-02): `release_cli/secrets.py` `get(name) -> str | None`: env `RELEASE_SECRET_<NAME>` (upper, `-`→`_`),
then macOS Keychain (`security find-generic-password -s release-cli -a <name> -w`), then `secret-tool` on Linux.
`release secret set|rm|ls <name>` (value read with getpass, never echoed). Connectors receive secret *names* in
config and resolve them themselves.

**Global config** (`~/.config/release/release.toml`): `[jira] base_url, email` (token: secret `jira-token`).
**Project config** (`.release`, `[connectors.jira]`): `project_key`, `rc_transition`, `final_transition`;
(`[connectors.slack-notify]`): `webhook_secret` (secret name), `notify_on` = `final` | `every-deploy`.

**Run results passed in `prior`:**
- `jira`: `{"issues": [{"key", "summary", "from", "to", "status": "moved|skipped|failed", "reason"}]}`
- `slack-notify`: `{"sent": bool, "reason"?}`. It reads `prior` of the deploy connector (environment), `github-release`
  (url) and `jira` (moved issues) when present.

Recommended order: build, deploy, `jira`, `github-release`, `slack-notify`, `cursor-review`.

## Phases and tasks
<!-- Release PR: release/jira-slack-p1 → main -->
### Phase 1: Jira and Slack built-ins
| Task | Repo | Depends on | Status | PR | Note |
|---|---|---|---|---|---|
| [T-01](tasks/T-01.md) `multi` question type | release-cli | — | todo | | |
| [T-02](tasks/T-02.md) secrets + `release secret` | release-cli | — | todo | | |
| [T-03](tasks/T-03.md) `jira` connector | release-cli | T-01, T-02 | todo | | |
| [T-04](tasks/T-04.md) `slack-notify` connector | release-cli | T-02 | todo | | reads T-03's result shape |

## Open questions
- Wait for the three 0.3 sessions (hotfix, doctor, history) to merge before dispatching: same files (`commands.py`, README).

## Log
- 2026-11-17: plan written (location, review checklist, per-project transition, Slack webhook decided by the user).
- 2026-11-17: user accepted the proposals: `notify_on` default `final`, Jira Cloud only, other projects' keys listed unticked.
