# release

Cut release candidates and final versions for Maven, Gradle, and sbt projects.

The CLI is `release`. It runs on macOS and Linux.

## Install

Requires Python 3.11+ and git. [uv](https://docs.astral.sh/uv/) is the recommended installer.

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/GutiNicolas/release-cli.git
cd release-cli
chmod +x install.sh
./install.sh
```

`install.sh` records the clone in `~/.config/release/source.toml` (remote, branch, path). After that, update from any directory:

```sh
release update              # git pull in the clone + uv tool install --force; prints old -> new version
release update --defaults   # accept the default of any new config key without asking
release --version           # installed version
```

`release update` needs the clone to be clean (it stops before pulling otherwise). Existing config is kept: only a global key this version adds, or one whose meaning changed, is asked once, and `config_version` is saved in `~/.config/release/release.toml`. Built-in connectors such as `cursor-review` update with the CLI. Connectors installed from a URL stay on their SHA (`release connector update <name>`). It never rewrites `connectors.toml`, and never touches a project's `.release`. Without `source.toml` it falls back to uv's install receipt; with neither, it tells you how to reinstall.

If `release` is not found:

```sh
export PATH="$HOME/.local/bin:$PATH"
```

Other install options:

```sh
uv tool install -e .                 # editable
python3 -m pip install --user .      # without uv
uv tool uninstall release-cli        # uninstall
```


Build tools (`mvn`, `gradle` / `./gradlew`, `sbt`) are only needed if you configure hooks that invoke them.

## Quick start

In the project you want to release:

```sh
release --init
release --dry-run -rc
release -rc
```

`--init` detects the build tool from files in the current directory (it does not run Maven, Gradle, or sbt):

- Maven: `pom.xml`
- Gradle: `settings.gradle`, `settings.gradle.kts`, `build.gradle`, `build.gradle.kts`
- sbt: `build.sbt` or `project/build.properties`

If more than one is present, you choose. Init writes a local **`.release`** file and offers to add it to `.gitignore`. To share defaults with collaborators, opt in to a committed `release.toml`; a local `.release` still overrides it.

Re-run detection with `release --init --force`. `--init` never creates a release.

### Gradle version location

Put the project version in `gradle.properties` (`version=…`) or as a top-level `version = "…"` / `version.set("…")` in the root `build.gradle` / `build.gradle.kts`. Another file works too if you set `version_file` in `.release`.

### Maven multi-module

The root `pom.xml` holds the version. Every module listed under `<modules>` (recursively; a directory with a `pom.xml` or a path to an `.xml` file) gets its `<parent><version>` bumped when that parent is the root or another module of the same build and it still has the old version. A module's own `<version>` is bumped only when it equals the old one. Modules whose parent is another project (e.g. `spring-boot-starter-parent`) are not touched, and missing module folders are skipped. `--dry-run` shows the diff of every pom.

## Versioning

| Command | Result |
|---|---|
| `release -rc` | If the version is already an RC, increment `rcN`. Otherwise start **next minor** as `rc0`. |
| `release -rc --minor` | Same as the default when starting a series |
| `release -rc --major` | `X+1.0.0-rc0` |
| `release -rc --patch` | `x.y.Z+1-rc0` |
| `release -rc 2.80.0` | `2.80.0-rc0` |
| `release -fv` | Require an RC; tag `X.Y.Z`; leave the project at `X.Y.Z-SNAPSHOT` |

`--major`, `--minor`, `--patch`, and an explicit version start a new series. If an RC is already open, `release` asks before replacing it in the version file (default **no**; existing tags stay). Declining, or `--defaults`, keeps the previous error: use `release -rc` to increment that candidate or `release -fv` to finalize it.

After a final, the next SNAPSHOT stays at the version you just shipped. The next bump happens on the following `-rc`.

Example from `2.74.0-rc2-SNAPSHOT`:

```sh
release --dry-run -rc   # 2.74.0-rc3
release -fv             # 2.74.0; next `release -rc` starts 2.75.0-rc0
release -rc --patch     # from 2.74.0-SNAPSHOT → 2.74.1-rc0
```

Tags created: `VERSION` and `{artifact}-{VERSION}`.

## Hooks

No test or publish command is built in. During init you can add zero or more commands:

```text
Add a command to run during release? (y/n) [n]: y
Command: mvn test
When? before / after [before]: before
Add another? (y/n) [n]: y
Command: mvn deploy
When? [before]: after
```

- **before** — runs before the version is changed (typical: tests). Failure aborts; the version file is untouched.
- **after** — runs after the release version is written (typical: publish). A deploy hooked as `before` would publish a SNAPSHOT.

Each release asks once per hook, with the configured command:

```text
Would you like to run [mvn test] before releasing? (y/n) [y]:
Would you like to run [mvn deploy] after setting version? (y/n) [y]:
```

`n` skips that hook for this run. Enter takes the hook's default (`y` unless the hook has `default = false`). `--dry-run` prints the plan, the diff of every file it would change, and the connector questions, writes nothing and runs no hooks. It then runs the same checks as a real release (clean tree, tags free locally and on origin, branch not behind origin) and exits 1 if one fails, so it works as a CI gate. `--skip-hooks` skips every hook (not connectors).

`--defaults` takes the configured default on every hook, connector, and `--init` question. It does not assume yes: a hook or connector question whose default is `n` stays `n`.

Add or change hooks without editing TOML:

```sh
release hook add --when before --cmd "mvn test"
release hook add --when after --cmd "mvn deploy" --default n
release hook add --when before --url https://example.com/check.sh   # downloaded once, runs as shell
release edit                                                        # add, remove, reorder, enable, default
```

## Hooks vs connectors

- **Hook.** Before the push. A shell command of this repo (Maven, Gradle, sbt, whatever you configure in `.release`). You can install it (`release hook add`) and edit it (`release edit`). It does not wait for CI and never sees a build result. A failing hook rolls the release back.
- **Connector.** After the push. A Python program that asks its own questions and returns a result that is passed to the next connector. It only starts once the tag is on the remote. Typical chain: `platform-build` (triggers the build of the tag and waits for `SUCCESS`), `platform-deploy`, `cursor-review`.

**The tag does not depend on the connectors.** Only an error up to the atomic push (hooks, writing the version, commit, tag, push) can undo a release; that is the rollback below. Once the push succeeded, the tag stays. A connector that fails never deletes, moves, or re-creates the tag.

### Install connectors

```sh
release connector add https://github.com/example/release-connector          # latest
release connector add https://github.com/example/release-connector@v0.1.0   # pinned
release connector add cursor-review                                                         # built in
release connector add github-release                                                        # built in
release connector add slack-notify                                                          # built in
release connector ls        # order, enabled, break_on_error, installed commit SHA
release connector update platform-build     # explicit; only a connector installed from a URL
release connector remove platform-deploy
```

`connector add` installs with `uv` into `~/.local/share/release/env`, reads the `release.connectors` entry points, and asks each new connector's position in the order, `break_on_error`, and whether it starts enabled. Installing again keeps the existing order. Installed code runs on your machine, like a shell hook.

The order and flags live in `~/.config/release/connectors.toml`. A project can override `order`, `enabled`, and `disabled` in `.release`:

```toml
[connectors]
order = ["platform-build", "platform-deploy"]
disabled = ["cursor-review"]

[connectors.platform-deploy]
jira_project_key = "DEMO"
```

Values a connector asks once per project (for example `version_tag`, `jira_project_key`, the cursor-review extra prompt) are saved under `[connectors.<name>]` the first time you answer. `release edit` shows each value and where it comes from, changes it, and can copy it to `release.toml` (`config share`) to share it with the team.

### Each run

After the push, every enabled connector asks its questions first (Enter takes that question's default, which the connector picks and can be `n`). Nothing runs until those answers are in. Then the connectors run in order, and each result is passed to the next as `prior`. `prior` during the questions is only a connector that already finished (a resume): a connector that needs an earlier result still asks up front and refuses at run time if that result is missing or failed.

- `break_on_error = true` (default for installed connectors): a failure stops the chain.
- `break_on_error = false` (default for `cursor-review`): the next connector runs and sees the failure.
- `--dry-run` shows the connector questions and never runs them. `--skip-connectors` skips the whole phase.
- With `--defaults`, a connector value that has to be typed once per project is never invented. If it is not saved yet, that connector fails and prints the `release edit config set ...` command to run.

### Resume

Each run writes `~/.local/share/release/runs/{repo}/{tag}.json`: which connector ran, `ok` / `failed` / `interrupted`, its `result` and `job_id`. `release connectors status <tag>` prints it, plus any pending review job.

The three typical cases:

1. **Build failed.** Fix the cause (or retry the Platform job), then run the chain again for the pushed tag: `release connectors run 1.5.0-rc0`.
2. **Deploy failed.** The build is fine; keep its result: `release connectors run 1.5.0-rc0 --from platform-deploy`. Earlier connectors are not re-run; their saved `result` is passed as `prior`.
3. **Ctrl-C (or a poll timeout) in the middle of a poll.** The connector is saved as `interrupted` with its `job_id`, and the resume command is printed. `release connectors run 1.5.0-rc0 --from platform-build` polls that same job again instead of starting another build.

`--only cursor-review` runs a single connector with every other saved result as `prior`. `connectors run` only works for a tag that already exists on `origin`.

### cursor-review

Optional, built in, off unless you enable it (`install.sh` asks once, default no). After a deploy reaches `SUCCESS` it schedules a local job (pid and state in `~/.local/share/release/jobs`) that runs 10 minutes after that time. The release does not wait, and closing the terminal does not kill the job. One job per repo and tag: a new one replaces the pending one. The target is wall-clock, so if the laptop slept past it, the review runs on wake. The job is a plain detached Python process (new session), the same on macOS and Linux.

The job does not run the review itself. It opens the Cursor app on a folder, waits for that window to come to the front, then prefills the chat via the documented deeplink `cursor://anysphere.cursor-deeplink/prompt?text=…` (`open -u` on macOS, `xdg-open` on Linux). The deeplink has no workspace param, so it must fire only after that window is frontmost. You confirm the prompt in the app; that is where MCP plugins live. It never calls `agent` / `cursor-agent`. The prompt carries the repo, version, environment, deploy type, `job_id`, and the `SUCCESS` time, plus an extra per-project prompt you set the first time (e.g. which skills or MCPs to use). When the chat is opened you get a desktop notification (`notify`, on by default): `osascript` on macOS, `notify-send` on Linux if it is installed (e.g. `libnotify-bin`); without it the handoff still runs, just without a notification. Output goes to the job's `.out` file.

Where it opens is `surface`, asked the first time you enable cursor-review (install or `release update`):

- `agents` (the default when you are asked): `cursor <folder>` with no `--classic`, `--new-window`, or `--reuse-window`. When the Agents window was the last Cursor window, that starts a new agent for the folder, under that project in the sidebar. Cursor has no flag that forces the Agents window; if the IDE was the last window, the same command opens the IDE instead.
- `classic`: `cursor --classic <folder>`, the IDE window for that folder.

`open_folder` is asked only for `agents`. Empty means the repo that was just deployed, so the chat shows under that project and uses its skills and MCPs. A path sends every review to that folder (its skills and MCPs, not the deployed repo's).

Settings: `release edit` then `global set cursor-review <key> <value>` for `delay_minutes`, `notify`, `surface` (`agents` or `classic`), `open_folder`, `cursor_bin`, `slack_cloud` (off), `cloud_command`. `slack_cloud` sends the review to a Cursor Cloud Agent instead, which is the only way to get the Slack DM; it runs in the cloud, so that agent needs the same integrations to see the errors, and `cloud_command` must be the command that starts it.

### github-release

Optional, built in: `release connector add github-release`. After the push it creates the GitHub Release of the tag with the [GitHub CLI](https://cli.github.com/) (`gh`, installed and logged in with `gh auth login`). Release candidates are marked as pre-releases. The notes are the commits since the previous tag (for a final, since the previous final), without the CLI's own `[manual-release]` commits; `release notes <tag>` prints the same text. It asks once per release (default yes), skips repositories whose `origin` is not on github.com, and on a resume reuses a release that already exists instead of creating a second one.

### slack-notify

Optional, built in: `release connector add slack-notify`. After the deploy reaches `SUCCESS` (right after the push when the chain has no deploy connector) it posts one message to a Slack channel through an [incoming webhook](https://api.slack.com/messaging/webhooks): repo, version, environment, deploy type, a link to the GitHub Release, the released Jira version with its delivered tickets, and the release notes (trimmed to Slack's limits). Put it after the deploy and `github-release` in the order so it can read their results.

The webhook URL is a secret: store it with `release secret set slack-webhook` (or the env var `RELEASE_SECRET_SLACK_WEBHOOK`). It is never written to `.release`, the run state, or the logs. The first time per project it asks:

- `webhook_secret`: the secret's *name* (default `slack-webhook`).
- `notify_on`: `final` (default; release candidates are not announced) or `every-deploy`.

Then, once per release, "Send the Slack notice?" (default yes). A missing secret fails the connector with the `release secret set <name>` command to run; resume with `release connectors run <tag> --from slack-notify`. The webhook must be `https`. A deploy connector named something other than `platform-deploy` is set with `deploy_connector` under `[connectors.slack-notify]`.

### Writing a connector

A connector is a `release.connectors` entry point pointing at a zero-arg callable. release-cli runs it in a subprocess with the action (`questions` or `run`) as `argv[1]`, one JSON request on stdin, and expects one JSON object on stdout. Logs go to stderr and are shown live.

- Request: `protocol`, `action`, `connector`, `repo`, `remote_url`, `repo_root`, `artifact`, `release_version`, `tags`, `sha`, `mode` (`rc`/`fv`), `dry_run`, `prior` (`{name: {status, result, error, job_id}}`), `config`, `resume_job_id`; `run` adds `answers`.
- `questions` → `{"protocol": 1, "questions": [...], "applies": true, "timeout_seconds": 1860}`. Not for this project: `"applies": false, "reason": "..."` (skipped, exit 0). A prerequisite that **already finished** unsuccessfully (e.g. a resumed build that is not `SUCCESS`): `"applies": false, "blocked": true, "reason": "..."` (recorded `failed` with that reason, exit 1). The CLI asks every connector before any `run`, so `prior` in `questions` does not include connectors that will run in this same invocation. If that result is simply not there yet, return the questions; `run` receives the real `prior` and is where a missing or failed prerequisite is refused. Question types `bool`, `choice`, `text`, `int`, `multi`, with `default`, `default_when`, `when`, `persist`, `pattern` (text), `min`/`max` (int). 30 s timeout.
- `multi` asks "pick from this list": `{"id": "issues", "type": "multi", "prompt": "...", "options": [{"value": "ABC-1", "label": "ABC-1 Fix login"}], "default": ["ABC-1"], "other": {"pattern": "[A-Z][A-Z0-9_]+-[0-9]+"}}`. `options` may be plain strings; `default` is a subset of the values (none if omitted); `other` (optional) lets the user add extra values matching `pattern`; `persist` must stay `none`. The answer in `run` is a list of strings: the picked options in option order, then the extras. In the terminal the user sees a numbered `[x]`/`[ ]` list: Enter accepts, `2 5` toggles, `+KEY` adds, `-KEY` removes. `--defaults` takes `default`.
- `run` → `{"protocol": 1, "ok": true, "result": {...}}`, optionally `status` (`interrupted`), `error`, `job_id`.
- Any other `protocol`, invalid JSON, missing fields, or a non-zero exit from `run` marks the connector `failed` with the reason. On timeout or Ctrl-C the CLI sends SIGINT and waits 10 s for an `interrupted` answer carrying the `job_id`.

The built-in [`release_cli/cursor_review.py`](release_cli/cursor_review.py) is a small complete example; [`release_cli/connectors.py`](release_cli/connectors.py) has the exact validation.

## Hotfix

Patch a version that is already released:

```sh
release hotfix 2.74.0     # branch hotfix/2.74.1 from tag 2.74.0 (also accepts <artifact>-2.74.0)
# fix, commit
release -rc               # 2.74.1-rc0
release -rc               # 2.74.1-rc1
release -fv               # 2.74.1; then merge hotfix/2.74.1 back into your main branch
```

Without this, `release -rc` on the commit of a final tag starts the next **minor** (`2.75.0-rc0`). On a `hotfix/X.Y.Z` branch a new series is a **patch** instead; `--minor`, `--major` or an explicit version still win. The command makes no commit: the version file at the tag already reads `2.74.0`.

`release hotfix` refuses when the tag is missing (it fetches it from `origin` if only the remote has it), is a release candidate, when the working tree is dirty, when `hotfix/X.Y.Z+1` already exists locally or on `origin`, when `X.Y.Z+1` (or one of its RCs) is already tagged, or when the version file at that tag does not read `X.Y.Z`. In every case nothing is created. It works for Maven (including multi-module), Gradle and sbt.

The first release on the branch pushes it to `origin`. After `-fv`, `release` reminds you to merge the branch back and does not do it. Expect a conflict in the version file: keep the main branch's version.

## Status, undo, notes

```sh
release status              # version, open RC, last tag, branch vs origin, the last tag's connectors
release status --json       # the same as one JSON object (--no-fetch skips the fetch)
release history             # releases of this repo, newest first, with what each connector did
release notes [tag]         # markdown notes since the previous tag (default: the latest tag)
release undo 2.75.0-rc1     # deletes that RC's two tags on origin and locally, after asking (--force skips)
```

`release history` lists the version tags of the repo (newest first, `--limit N`, default 10; `--finals` hides release candidates; `--json` prints one object) with the tag date and commit. Under each release it shows the saved connector runs of this machine: status, `updated_at`, the error, and for a connector that is not `ok` the command that resumes it. A tag with no saved runs shows `no connector runs on this machine`. It only reads.

`release undo` only takes release candidates: a final may already be used by someone. It does not rewrite history. The version commits stay, so the next `release -rc` creates the next number (`rc2`). It cancels a pending cursor-review of that tag and forgets its saved connector runs. A GitHub Release or a deploy made from the tag is not undone; remove it yourself (`gh release delete <tag>`).

## Doctor

```sh
release doctor              # why release would not work here; changes nothing
release doctor --json       # the same as a JSON list of {name, status, message, fix}
```

Each line is `ok`, `warn` or `FAIL`, with what to do next. It checks:

- **Tools:** `git`, `uv` (connectors and `release update`), and `gh` plus `gh auth status` when the `github-release` connector is installed.
- **Install:** `source.toml` (or the uv receipt) points at a git clone, so `release update` works, and the installed version against the clone's `pyproject.toml`.
- **Global config:** `connectors.toml`, `release.toml` and `source.toml` parse. Every installed connector resolves: external ones need their environment and an entry point that loads (doctor does not run the connector).
- **Project** (when `.release` or `release.toml` is in the current directory): the config parses, the adapter reads the version, the directory is a git repo on a branch, `origin` is reachable and the branch is not behind it (doctor asks origin with `git ls-remote`; it never fetches), and the project's connector overrides name installed connectors.

The exit code is 1 if any line fails. Warnings do not change it.

## Secrets

Tokens and webhook URLs (for example a Jira API token or a Slack webhook) never go in `.release`, `release.toml`, `connectors.toml`, run state or a log line. Config files hold a secret's *name*; the connector looks the value up when it runs.

```sh
release secret set jira-token   # asks for the value, hidden; it is never a command-line argument
release secret ls               # names only
release secret rm jira-token
```

A secret is found in this order: the environment variable `RELEASE_SECRET_<NAME>` (upper case, `-` becomes `_`, so `RELEASE_SECRET_JIRA_TOKEN`; use it in CI), then the macOS Keychain, then `secret-tool` (Linux keyring). `release secret set` stores in the Keychain on macOS and in the keyring on Linux. Without either, set the environment variable instead.

## CI and scripts

- `--json`: stdout carries exactly one JSON object, also on errors (`{"ok": false, "error": ..., "exit_code": ...}`). Progress, prompts, and hook output go to stderr. On success: `tool`, `artifact`, `version`, `next_snapshot`, `tags`, `sha`, `connectors` (name → status); with `--dry-run`, `files` instead of `sha`.
- `--quiet` / `-q`: only the result line (`RELEASE <artifact>-<version> FINISHED!`) and errors. Hook output goes to stderr.
- Combine with `--defaults` so nothing waits for an answer. The exit code is 0 on success, 1 on failure, 130 on Ctrl-C.

## Git

Before any hook runs, the release checks that the working tree is clean, the tags do not exist locally or on `origin`, and the branch is not behind `origin` (it fetches first). Otherwise an `after` hook such as `mvn deploy` could publish and then the push would be rejected. Commits and tags are pushed with a single `git push --atomic`. If a hook, the version write, a commit, or the push fails, or you press Ctrl-C before the push, the version files are restored, unpushed local commits are reset, and local tags are deleted. A Ctrl-C during the push checks `origin`: if the tag is there, the release stands. After the push nothing is rolled back.

## License

[GPL-3.0-only](LICENSE)
