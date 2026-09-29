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
```

`release update` needs the clone to be clean (it stops before pulling otherwise). Existing config is kept: only a global key this version adds, or one whose meaning changed, is asked once, and `config_version` is saved in `~/.config/release/release.toml`. It never updates connectors (`release connector update <name>`), never rewrites `connectors.toml`, and never touches a project's `.release`. Without `source.toml` it falls back to uv's install receipt; with neither, it tells you how to reinstall.

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

Put the project version in `gradle.properties` (`version=…`) or as a top-level `version = "…"` / `version.set("…")` in the root `build.gradle` / `build.gradle.kts`.

## Versioning

| Command | Result |
|---|---|
| `release -rc` | If the version is already an RC, increment `rcN`. Otherwise start **next minor** as `rc0`. |
| `release -rc --minor` | Same as the default when starting a series |
| `release -rc --major` | `X+1.0.0-rc0` |
| `release -rc --patch` | `x.y.Z+1-rc0` |
| `release -rc 2.80.0` | `2.80.0-rc0` |
| `release -fv` | Require an RC; tag `X.Y.Z`; leave the project at `X.Y.Z-SNAPSHOT` |

`--major`, `--minor`, `--patch`, and an explicit version apply only when **starting** a series. During an RC they error: use `release -rc` or `release -fv`.

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

`n` skips that hook for this run. Enter takes the hook's default (`y` unless the hook has `default = false`). `--dry-run` prints the plan and does not write or run hooks. `--skip-hooks` skips every hook (not connectors).

`--defaults` takes the configured default on every hook, connector, and `--init` question. It does not assume yes: a hook or connector question whose default is `n` stays `n`. `-y` / `--yes` still work for one more version as a deprecated alias of `--defaults` and print a warning.

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
release connector ls        # order, enabled, break_on_error, installed commit SHA
release connector update platform-build     # explicit; a release never updates connectors
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

After the push, each enabled connector shows its questions (Enter takes that question's default, which the connector picks and can be `n`), then runs. Results pass forward as `prior`.

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

The job does not run the review itself. It pins the already-open classic editor window for the absolute repo path (`cursor --classic <abs-repo>`; no `--reuse-window` / `--new-window`, so it does not steal another project or open the Agents clone UI), waits for that window to come to the front, then prefills Agent chat via the documented deeplink `cursor://anysphere.cursor-deeplink/prompt?text=…` (`open -u` on macOS, `xdg-open` on Linux). The deeplink has no workspace param, so it must fire only after that window is frontmost. You confirm the prompt in the app; that is where MCP plugins live. It never calls `agent` / `cursor-agent`. The prompt carries the repo, version, environment, deploy type, `job_id`, and the `SUCCESS` time, plus an extra per-project prompt you set the first time (e.g. which skills or MCPs to use). When the chat is opened you get a desktop notification (`notify`, on by default): `osascript` on macOS, `notify-send` on Linux if it is installed (e.g. `libnotify-bin`); without it the handoff still runs, just without a notification. Output goes to the job's `.out` file.

Settings: `release edit` then `global set cursor-review <key> <value>` for `delay_minutes`, `notify`, `cursor_bin`, `slack_cloud` (off), `cloud_command`. `slack_cloud` sends the review to a Cursor Cloud Agent instead, which is the only way to get the Slack DM; it runs in the cloud, so that agent needs the same integrations to see the errors, and `cloud_command` must be the command that starts it.

### Writing a connector

A connector is a `release.connectors` entry point pointing at a zero-arg callable. release-cli runs it in a subprocess with the action (`questions` or `run`) as `argv[1]`, one JSON request on stdin, and expects one JSON object on stdout. Logs go to stderr and are shown live.

- Request: `protocol`, `action`, `connector`, `repo`, `remote_url`, `repo_root`, `artifact`, `release_version`, `tags`, `sha`, `mode` (`rc`/`fv`), `dry_run`, `prior` (`{name: {status, result, error, job_id}}`), `config`, `resume_job_id`; `run` adds `answers`.
- `questions` → `{"protocol": 1, "questions": [...], "applies": true, "timeout_seconds": 1860}`. Not for this project: `"applies": false, "reason": "..."` (skipped, exit 0). A prerequisite in `prior` failed (e.g. no `SUCCESS` build): `"applies": false, "blocked": true, "reason": "..."` (recorded `failed` with that reason, exit 1). Question types `bool`, `choice`, `text`, `int`, with `default`, `default_when`, `when`, `persist`, `pattern` (text), `min`/`max` (int). 30 s timeout.
- `run` → `{"protocol": 1, "ok": true, "result": {...}}`, optionally `status` (`interrupted`), `error`, `job_id`.
- Any other `protocol`, invalid JSON, missing fields, or a non-zero exit from `run` marks the connector `failed` with the reason. On timeout or Ctrl-C the CLI sends SIGINT and waits 10 s for an `interrupted` answer carrying the `job_id`.

The built-in [`release_cli/cursor_review.py`](release_cli/cursor_review.py) is a small complete example; [`release_cli/connectors.py`](release_cli/connectors.py) has the exact validation.

## Git

The working tree must be clean and the tags must not already exist. Commits and tags are pushed with a single `git push --atomic`. If a hook, the version write, a commit, or the push fails, version files are restored, unpushed local commits are reset, and local tags are deleted. After the push nothing is rolled back.

## License

[GPL-3.0-only](LICENSE)
