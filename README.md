<h1 align="center">release</h1>

<p align="center">
  Cut release candidates and final versions from your terminal.<br>
  One command bumps the version, tags, and pushes. If anything fails before the push, it is all undone.
</p>

<p align="center">
  <a href="https://github.com/GutiNicolas/release-cli/actions/workflows/test.yml"><img alt="tests" src="https://github.com/GutiNicolas/release-cli/actions/workflows/test.yml/badge.svg"></a>
  <a href="LICENSE"><img alt="License: GPL-3.0" src="https://img.shields.io/badge/license-GPL--3.0-blue"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white">
  <img alt="macOS and Linux" src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux-555">
  <img alt="No dependencies" src="https://img.shields.io/badge/dependencies-none-brightgreen">
</p>

<p align="center">
  <a href="#maven"><img alt="Maven" src="https://img.shields.io/badge/Maven-C71A36?logo=apachemaven&logoColor=white"></a>
  <a href="#gradle"><img alt="Gradle" src="https://img.shields.io/badge/Gradle-02303A?logo=gradle&logoColor=white"></a>
  <a href="#sbt"><img alt="sbt" src="https://img.shields.io/badge/sbt-DC322F?logo=scala&logoColor=white"></a>
  <a href="#go"><img alt="Go" src="https://img.shields.io/badge/Go-00ADD8?logo=go&logoColor=white"></a>
</p>

```console
$ release -rc
STARTING RELEASE
TOOL: maven
CURRENT VERSION: 1.4.0-SNAPSHOT
PROJECT ARTIFACT: example-app
RELEASE VERSION: 1.5.0-rc0
NEXT SNAPSHOT: 1.5.0-rc0-SNAPSHOT
TAGS: 1.5.0-rc0, example-app-1.5.0-rc0
HOOKS: none
RELEASE example-app-1.5.0-rc0 FINISHED!

$ release -fv
...
RELEASE example-app-1.5.0 FINISHED!
left project at 1.5.0-SNAPSHOT (already released). next `release -rc` starts 1.6.0-rc0
```

## Contents

- [What a release does](#what-a-release-does)
- [Install](#install)
- [Quick start](#quick-start)
- [Supported projects](#supported-projects)
- [Versioning](#versioning)
- [Commands](#commands)
- [Hooks](#hooks)
- [Connectors](#connectors)
- [Hotfix](#hotfix)
- [Status, history, notes, undo](#status-history-notes-undo)
- [Doctor](#doctor)
- [Secrets](#secrets)
- [CI and scripts](#ci-and-scripts)
- [How a release stays safe](#how-a-release-stays-safe)
- [Development](#development)

## What a release does

`release -rc` (a release candidate) or `release -fv` (the final version) runs these steps in order:

1. **Checks** that the working tree is clean, the tags are free locally and on `origin`, and the branch is not behind `origin`.
2. Runs your **`before` hooks**, such as `mvn test`.
3. Writes the release version into the version file (only that field) and runs your **`after` hooks**, such as `mvn deploy`.
4. Commits, creates the tags, moves the project to the next `-SNAPSHOT`, and commits again.
5. Pushes the commits and the tags together in one `git push --atomic`.
6. Runs your **connectors**: GitHub Release, Go builds, Homebrew, npm, signing, Jira, Slack, or your own.

If any step up to the push fails (or you press Ctrl-C), your files, commits and tags go back to how they were. Once the push succeeds the tag stays, even if a connector fails later.

## Install

You need macOS or Linux and git. The build tools (`mvn`, `gradle` / `./gradlew`, `sbt`, `go`) are only needed by the hooks and connectors that call them.

### Homebrew (macOS and Linux)

```sh
brew tap GutiNicolas/tap
brew trust --formula GutiNicolas/tap/release-cli
brew install GutiNicolas/tap/release-cli
release --version
```

Homebrew 7 and later only load formulae from outside its own taps once you trust them, which is what `brew trust` does. On an older Homebrew that has no `brew trust`, skip that line.

Homebrew brings its own Python. The built-in connectors work as they are; a connector installed from a URL (`release connector add <url>`) also needs [uv](https://docs.astral.sh/uv/): `brew install uv`.

Update:

```sh
brew upgrade release-cli
release update   # asks only the settings the new version adds
```

Uninstall:

```sh
brew uninstall release-cli
brew untrust --formula GutiNicolas/tap/release-cli
brew untap GutiNicolas/tap
```

If you installed from a clone before, run `uv tool uninstall release-cli` first: otherwise the old `release` in `~/.local/bin` comes first in your `PATH`. Your settings and connectors are kept.

### From a clone

You need Python 3.11 or later and [uv](https://docs.astral.sh/uv/).

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh     # skip if you already have uv
git clone https://github.com/GutiNicolas/release-cli.git
cd release-cli
./install.sh
release --version
```

`install.sh` installs the `release` command with `uv tool install` and remembers where the clone is, so keep the clone. If your shell says `release: command not found`, add uv's bin folder to your `PATH` (in `~/.zshrc` or `~/.bashrc`) and open a new terminal:

```sh
export PATH="$HOME/.local/bin:$PATH"
```

Update:

```sh
release update              # pulls the clone and reinstalls, from any directory; prints old -> new version
release update --defaults   # takes the default of any new setting instead of asking
```

The clone must have no local changes. Your settings are kept: only a setting this version adds, or one whose meaning changed, is asked once. Built-in connectors update with the CLI. Connectors installed from a URL stay on their commit until `release connector update <name>`. A project's `.release` is never touched.

Uninstall:

```sh
uv tool uninstall release-cli
```

Then delete the clone.

### With uv, without a clone

```sh
uv tool install git+https://github.com/GutiNicolas/release-cli
uv tool upgrade release-cli && release update   # update
uv tool uninstall release-cli                   # uninstall
```

### Remove your settings

Every channel keeps your settings, connectors and saved runs in the same place. To remove them too:

```sh
rm -rf ~/.config/release ~/.local/share/release
```

## Quick start

In the project you want to release:

```sh
release --init          # detects the build tool and writes .release; offers test and deploy hooks
release --dry-run -rc   # shows the version, the tags and the diff of every file; changes nothing
release -rc             # 1.5.0-rc0: commit, tags, push
release -rc             # 1.5.0-rc1: the next candidate
release -fv             # 1.5.0: the final, from the open candidate
```

`--init` reads the files in the folder (it never runs Maven, Gradle, sbt or Go) and asks when it finds more than one build tool. It writes a local `.release` file and offers to add it to `.gitignore`. To share the settings with your team, opt in to a committed `release.toml`; a local `.release` still overrides it. Run `release --init --force` to detect again. `--init` never creates a release.

## Supported projects

| Build tool | Detected by | Version lives in | Tags by default |
|---|---|---|---|
| <a id="maven"></a>**Maven** | `pom.xml` | the root `pom.xml` `<version>` | `1.5.0`, `example-app-1.5.0` |
| <a id="gradle"></a>**Gradle** | `settings.gradle(.kts)`, `build.gradle(.kts)` | `gradle.properties` (`version=`) or the root build file | `1.5.0`, `example-app-1.5.0` |
| <a id="sbt"></a>**sbt** | `build.sbt`, `project/build.properties` | `version := "…"` in `version.sbt` or `build.sbt` | `1.5.0`, `example-app-1.5.0` |
| <a id="go"></a>**Go** | `go.mod` | a `VERSION` file at the repo root | `v1.5.0` |

Any tool can read the version from another file: set `version_file` in `.release`.

**Maven multi-module.** The root `pom.xml` holds the version. Every module listed under `<modules>` (recursively; a directory with a `pom.xml` or a path to an `.xml` file) gets its `<parent><version>` bumped when that parent is the root or another module of the same build and it still has the old version. A module's own `<version>` is bumped only when it equals the old one. Modules whose parent is another project (for example `spring-boot-starter-parent`) are not touched, and missing module folders are skipped. `--dry-run` shows the diff of every pom.

**Gradle.** Put the version in `gradle.properties` (`version=…`) or as a top-level `version = "…"` / `version.set("…")` in the root `build.gradle` / `build.gradle.kts`.

**Go.** The version lives in a plain `VERSION` file: one line, `X.Y.Z`, `X.Y.Z-rcN`, with or without `-SNAPSHOT`, and nothing else (anything else is refused, and the message shows the line it read). `release --init` in a folder with `go.mod`:

- sets `tool = "go"` and takes the artifact from the last part of the module path (`github.com/acme/hello` is `hello`; a trailing `/v2` is skipped);
- sets `tag_format = ["v{version}"]`, the form `go install ...@v0.1.0` and the Go module proxy expect;
- after asking, creates `VERSION` with `0.0.0-SNAPSHOT` when it is missing, so the first `release -rc` is `v0.1.0-rc0`. Commit `VERSION` before the first release;
- offers `go test ./...` as a `before` hook.

A release does not build anything by itself. To ship binaries, add the [`go-build`](docs/connectors.md#go-build) connector, and then [`github-release`](docs/connectors.md#github-release), [`sign`](docs/connectors.md#sign), [`homebrew-tap`](docs/connectors.md#homebrew-tap) and [`npm-publish`](docs/connectors.md#npm-publish) as you need them.

## Versioning

| Command | Result |
|---|---|
| `release -rc` | If the version is already a candidate, the next one (`rc1`, `rc2`…). Otherwise the **next minor** as `rc0`. |
| `release -rc --minor` | Same as the default when starting a series |
| `release -rc --major` | `X+1.0.0-rc0` |
| `release -rc --patch` | `X.Y.Z+1-rc0` |
| `release -rc 2.80.0` | `2.80.0-rc0` |
| `release -fv` | Needs an open candidate. Tags `X.Y.Z` and leaves the project at `X.Y.Z-SNAPSHOT` |

Example from `2.74.0-rc2-SNAPSHOT`:

```sh
release --dry-run -rc   # 2.74.0-rc3
release -fv             # 2.74.0; the next `release -rc` starts 2.75.0-rc0
release -rc --patch     # from 2.74.0-SNAPSHOT: 2.74.1-rc0
```

`--major`, `--minor`, `--patch` and an explicit version start a new series. If a candidate is already open, `release` asks before replacing it (default **no**; existing tags stay). Declining, or `--defaults`, stops with an error: run `release -rc` to make the next candidate or `release -fv` to finalize it.

After a final, the next SNAPSHOT stays at the version you just shipped. The next bump happens on the following `-rc`.

### Tag names

By default a release creates two tags: `VERSION` and `{artifact}-{VERSION}`. `tag_format` in `.release` or `release.toml` changes that. It is a list of templates with `{version}` (required, once) and `{artifact}` (optional):

```toml
tag_format = ["v{version}"]     # only v2.75.0-rc1, v2.75.0 (what Go modules need)
```

- Every template becomes a tag, and they are all pushed together in one atomic push.
- The **first** template is the primary tag: `release notes`, `release history`, `release status`, the saved connector runs and the `tag` connectors receive all use it. `release_version` stays the bare version, without any prefix.
- Commands that take a tag (`connectors run|status`, `undo`, `hotfix`, `notes`) accept any form in the list. A `v` is understood only when a template has it: with `["v{version}"]`, `2.75.0-rc1` is not a tag.
- An invalid list (empty, no `{version}`, `{version}` twice, repeated templates, unknown `{placeholders}`, characters git rejects) is refused when the config loads, with the reason.
- If you change `tag_format` in a repo that already has releases, history and notes follow the new primary tag only, so the older tags no longer show up there.

## Commands

| Command | What it does |
|---|---|
| `release -rc` / `release -fv` | Create the next candidate / finalize the open one |
| `release --dry-run -rc` | Show the plan, the diff and the checks; change nothing (exit 1 if a check fails) |
| `release --init` | Detect the build tool and write `.release` |
| `release status` | Version, open candidate, last tag, branch vs `origin`, the last tag's connectors |
| `release history` | Releases of this repo, newest first, with what each connector did |
| `release notes [tag]` | Markdown notes: the commits since the previous tag |
| `release undo <rc-tag>` | Delete a candidate's tags locally and on `origin` |
| `release hotfix <X.Y.Z>` | Branch `hotfix/X.Y.Z+1` from a released tag |
| `release hook add` / `release edit` | Add hooks; change hooks, connector settings and saved values |
| `release connector add\|ls\|update\|remove` | Install and order connectors |
| `release connectors run\|status <tag>` | Resume or inspect the connectors of a pushed tag |
| `release secret set\|ls\|rm` | Store tokens and webhook URLs in the OS keychain |
| `release doctor` | Explain why `release` would not work here; changes nothing |
| `release update` | Update release-cli itself |

Useful flags: `--defaults` (never ask; take each default), `--skip-hooks`, `--skip-connectors`, `--json`, `-q`. Run `release --help` for the full list.

## Hooks

There is no built-in test or publish command. Hooks are your own shell commands, run around the version change:

- **before** runs before the version changes (typical: tests). If it fails, the release stops and nothing was changed.
- **after** runs once the release version is written (typical: publish). A deploy hooked as `before` would publish a SNAPSHOT.

`release --init` offers to add them:

```text
Add a command to run during release? (y/n) [n]: y
Command: mvn test
When? before / after [before]: before
Add another? (y/n) [n]: y
Command: mvn deploy
When? [before]: after
```

Each release asks once per hook (Enter takes the hook's default):

```text
Would you like to run [mvn test] before releasing? (y/n) [y]:
Would you like to run [mvn deploy] after setting version? (y/n) [y]:
```

Add or change hooks without editing TOML:

```sh
release hook add --when before --cmd "mvn test"
release hook add --when after --cmd "mvn deploy" --default n
release hook add --when before --url https://example.com/check.sh   # downloaded once, runs as shell
release edit                                                        # add, remove, reorder, enable, default
```

`--defaults` takes each hook's default and never assumes yes: a hook whose default is `n` stays skipped. `--skip-hooks` skips every hook (not connectors).

## Connectors

Connectors run **after** the push, in the order you choose, and each one receives the results of the ones before it. A connector that fails never touches the tag, and you can resume the chain from where it stopped.

| Connector | What it does |
|---|---|
| [`github-release`](docs/connectors.md#github-release) | Creates the GitHub Release with the notes (candidates as pre-releases) and uploads the build's files |
| [`go-build`](docs/connectors.md#go-build) | Cross-compiles a Go app from the tagged commit: archives, `checksums.txt` and a `curl \| sh` installer |
| [`sign`](docs/connectors.md#sign) | Signs the build with cosign and adds SLSA provenance anyone can verify |
| [`homebrew-tap`](docs/connectors.md#homebrew-tap) | Commits a Homebrew formula to your tap, so `brew install owner/tap/app` works |
| [`npm-publish`](docs/connectors.md#npm-publish) | Publishes an npm package with the same version (candidates under `next`) |
| [`jira`](docs/connectors.md#jira) | Checks the tickets in the release, and on a final sets the fix version and releases it |
| [`slack-notify`](docs/connectors.md#slack-notify) | Posts the release to a Slack channel |
| [`cursor-review`](docs/connectors.md#cursor-review) | Opens a Cursor chat to review the deploy, 10 minutes after it succeeds |

```sh
release connector add github-release                                  # built in
release connector add https://github.com/example/release-connector    # your own, from a git URL (@ref to pin)
release connector ls                                                  # order, enabled, break_on_error
release connectors run 1.5.0-rc0 --from platform-deploy               # resume after a failure
```

See [docs/connectors.md](docs/connectors.md) for each connector's settings, resuming, the dry run, and how to [write your own](docs/connectors.md#writing-a-connector).

## Hotfix

Patch a version that is already released:

```sh
release hotfix 2.74.0     # branch hotfix/2.74.1 from tag 2.74.0 (any form of tag_format: also <artifact>-2.74.0 by default)
# fix, commit
release -rc               # 2.74.1-rc0
release -rc               # 2.74.1-rc1
release -fv               # 2.74.1; then merge hotfix/2.74.1 back into your main branch
```

Without this, `release -rc` on the commit of a final tag starts the next **minor** (`2.75.0-rc0`). On a `hotfix/X.Y.Z` branch a new series is a **patch** instead; `--minor`, `--major` or an explicit version still win. The command makes no commit: the version file at the tag already reads `2.74.0`.

`release hotfix` creates nothing and stops when the tag is missing (it fetches it from `origin` if only the remote has it) or is a candidate, when the working tree is dirty, when `hotfix/X.Y.Z+1` already exists locally or on `origin`, when `X.Y.Z+1` (or one of its candidates) is already tagged, or when the version file at that tag does not read `X.Y.Z`.

The first release on the branch pushes it to `origin`. After `-fv`, `release` reminds you to merge the branch back and does not do it. Expect a conflict in the version file: keep the main branch's version.

## Status, history, notes, undo

```sh
release status              # version, open candidate, last tag, branch vs origin, the last tag's connectors
release status --json       # the same as one JSON object (--no-fetch skips the fetch)
release history             # releases of this repo, newest first (--limit N, --finals, --json)
release notes [tag]         # markdown notes since the previous tag (default: the latest tag)
release undo 2.75.0-rc1     # deletes that candidate's tags on origin and locally, after asking (--force skips)
```

`release history` shows, under each release, the connector runs saved on this machine: status, time, the error, and for a connector that did not finish, the command that resumes it. It only reads.

`release undo` only takes candidates, because someone may already use a final. It does not rewrite history: the version commits stay, so the next `release -rc` creates the next number. It cancels a pending cursor-review of that tag and forgets its saved connector runs. A GitHub Release or a deploy made from the tag is not undone; remove it yourself (`gh release delete <tag>`).

## Doctor

```sh
release doctor              # why release would not work here; changes nothing
release doctor --json       # the same as a JSON list of {name, status, message, fix}
```

Each line is `ok`, `warn` or `FAIL`, with what to do next. It checks:

- **Tools:** `git`, `uv` (connectors installed from a URL, and `release update` from a clone), and `gh` plus `gh auth status` when `github-release` is installed.
- **Install:** how release-cli was installed (Homebrew, uv or a clone); for a clone, the clone `release update` uses and the installed version against the clone's.
- **Global config:** `connectors.toml`, `release.toml` and `source.toml` parse, and every installed connector loads (doctor does not run it).
- **Project** (when `.release` or `release.toml` is in the current folder): the config parses, the version can be read, the folder is a git repo on a branch, `origin` is reachable and the branch is not behind it (doctor never fetches), and the project's connector settings name installed connectors.

The exit code is 1 if any line fails. Warnings do not change it.

## Secrets

Tokens and webhook URLs (a Jira API token, a Slack webhook, an npm token) never go in `.release`, `release.toml`, `connectors.toml`, the run state or a log line. Config files hold a secret's *name*; the connector looks the value up when it runs.

```sh
release secret set jira-token   # asks for the value, hidden; it is never a command-line argument
release secret ls               # names only
release secret rm jira-token
```

A secret is looked up in this order: the environment variable `RELEASE_SECRET_<NAME>` (upper case, `-` becomes `_`, so `RELEASE_SECRET_JIRA_TOKEN`; use it in CI), then the macOS Keychain, then `secret-tool` (the Linux keyring). `release secret set` stores in the Keychain on macOS and in the keyring on Linux. Without either, set the environment variable instead.

## CI and scripts

- `--json`: stdout carries exactly one JSON object, also on errors (`{"ok": false, "error": ..., "exit_code": ...}`). Progress, prompts and hook output go to stderr. On success: `tool`, `artifact`, `version`, `next_snapshot`, `tags`, `sha`, `connectors` (name → status); with `--dry-run`, `files` instead of `sha`.
- `--quiet` / `-q`: only the result line (`RELEASE <artifact>-<version> FINISHED!`) and errors.
- `--defaults` so nothing waits for an answer.
- `release --dry-run -rc` runs the same checks as a real release and exits 1 if one fails, so it works as a CI gate.
- Exit codes: 0 on success, 1 on failure, 130 on Ctrl-C.

## How a release stays safe

Before any hook runs, the release checks that the working tree is clean, the tags do not exist locally or on `origin`, and the branch is not behind `origin` (it fetches first). Otherwise an `after` hook such as `mvn deploy` could publish, and then the push would be rejected.

Commits and tags are pushed with a single `git push --atomic`. If a hook, the version write, a commit or the push fails, or you press Ctrl-C before the push, the version files are restored, the unpushed commits are reset and the local tags are deleted. A Ctrl-C during the push checks `origin`: if the tag is there, the release stands. After the push nothing is rolled back.

release-cli's own commits start with `[release-cli]` (for example `[release-cli] Prepare release 1.5.0`), so release notes leave them out.

## Development

```sh
git clone https://github.com/GutiNicolas/release-cli.git && cd release-cli
uv tool install -e .                  # editable install: `release` runs your working copy
uv run --with pytest pytest -q        # tests
uvx ruff check .                      # lint
```

release-cli has no runtime dependencies beyond the Python standard library.

## License

[GPL-3.0-only](LICENSE)
