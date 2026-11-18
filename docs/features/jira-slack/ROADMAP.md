# Jira transitions and Slack notice: roadmap

## Goal
A final release can only reach prd when Jira already has it delivered: before the deploy, the release's tickets get
the fixVersion `{artifact}-{X.Y.Z}`, move to the delivered status, and that Jira version is released. The team then
gets a Slack message with what shipped. Both are built-in connectors of release-cli.

## Decisions
- Built-ins in release-cli (`jira`, `slack-notify`), like `cursor-review` and `github-release` — they update with
  `release update`. The repo is public: the code is generic; URLs, keys, workflows and channels live in config.
- Tickets come from the commits between the previous tag and this one (same range as `release notes`), shown as a
  checklist with every ticket of the project's key ticked; the user unticks or adds keys before anything runs.
  This needs a new protocol question type `multi` (backward compatible addition to protocol 1).
- The deploy platform refuses a prd deploy unless the Jira version named exactly `{artifact}-{X.Y.Z}` (e.g.
  `fraud-atlas-1.0.0` in project `FRFLOW`) exists, is released, and its tickets are in the delivered status
  ("Entregado"). Jira cannot release a version without tickets.
- RC = qa, final = prd (the deploy connector still asks its own environment). On an RC the `jira` connector writes
  nothing and never blocks: it only warns about tickets not in an allowed qa status (default `Code Review`,
  `Code Review Approved`, per project).
- On a final, before the deploy: create the version if missing, set it as fixVersion of every selected ticket, move
  each ticket to the delivered status with the project's transition (read from that workflow once, saved in
  `.release`; a ticket already delivered is left alone), then release the version. Any ticket that cannot be
  delivered, or no tickets at all, fails the connector (`break_on_error`), so the deploy does not run; resume with
  `release connectors run <tag> --from jira` after fixing it in Jira or unticking it.
- Final ticket range: commits since the previous final (all its RCs included).
- Slack via incoming webhook only (no bot token). Teams is out.
- Secrets (Jira API token, Slack webhook URL) never go into TOML, run state, `prior`, or logs: env var first, then
  the OS keychain, set with `release secret set <name>`.
- `slack-notify` notifies finals by default (`notify_on = "final"`); `every-deploy` is the per-project opt-in.
- Jira Cloud only (REST v3, email + API token). Data Center is out.
- Ticket keys of another project found in the commits are listed unticked, not hidden.
- Questions are asked before any connector runs (CLI rule). `jira` runs before the deploy connector; `slack-notify`
  runs after it and only when the deploy reports `SUCCESS` (right after the push when there is no deploy connector).

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
**Project config** (`.release`, `[connectors.jira]`): `project_key`, `delivered_transition` (name of the transition to
the delivered status), `qa_statuses` (list, default `["Code Review", "Code Review Approved"]`);
(`[connectors.slack-notify]`): `webhook_secret` (secret name), `notify_on` = `final` | `every-deploy`.

**Run results passed in `prior`:**
- `jira`: `{"mode": "rc|fv", "jira_version": "fraud-atlas-1.0.0", "released": bool, "url",
  "issues": [{"key", "summary", "from", "to", "status": "delivered|already|failed|warned|ok", "reason"}]}`
- `slack-notify`: `{"sent": bool, "reason"?}`. It reads `prior` of the deploy connector (environment), `github-release`
  (url) and `jira` (moved issues) when present.

Required order: build, `jira`, deploy, `github-release`, `slack-notify`, `cursor-review` (`jira` must precede the deploy).

## Phases and tasks
<!-- Release PR: release/jira-slack-p1 → main (branch cut from main 3f219cb) -->
### Phase 1: Jira and Slack built-ins
| Task | Repo | Depends on | Status | PR | Note |
|---|---|---|---|---|---|
| [T-01](tasks/T-01.md) `multi` question type | release-cli | — | integrated | #12 | |
| [T-02](tasks/T-02.md) secrets + `release secret` | release-cli | — | integrated | #11 | |
| [T-03](tasks/T-03.md) `jira` connector | release-cli | T-01, T-02 | integrated | #14 | real-Jira manual check not run |
| [T-04](tasks/T-04.md) `slack-notify` connector | release-cli | T-02 | integrated | #13 | reads T-03's result shape |

## Open questions
- How the deploy platform reads the Jira version (exact name match, project from `jira_project_key`) is taken from the
  user; T-03's manual check on a real FRFLOW release confirms it.

## Log
- 2026-11-17: T-04 (#13) and T-03 (#14) squash-merged into release/jira-slack-p1 (README and BUILTINS conflict resolved once); version 0.3.0.
- 2026-11-17: plan written (location, review checklist, per-project transition, Slack webhook decided by the user).
- 2026-11-18: T-03 and T-04 dispatched.
- 2026-11-17: T-01 (#12) and T-02 (#11) squash-merged into release/jira-slack-p1; T-03 and T-04 are ready to dispatch.
- 2026-11-17: requirement change: Jira delivery is a prd prerequisite (fixVersion `{artifact}-{X.Y.Z}` released,
  tickets "Entregado"), `jira` runs before the deploy, RCs only warn. T-01 and T-02 dispatched.
- 2026-11-17: user accepted the proposals: `notify_on` default `final`, Jira Cloud only, other projects' keys listed unticked.
