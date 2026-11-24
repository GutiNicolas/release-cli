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

`--init` detects the build tool from files in the current directory (it does not run Maven, Gradle, sbt, or Go):

- Maven: `pom.xml`
- Gradle: `settings.gradle`, `settings.gradle.kts`, `build.gradle`, `build.gradle.kts`
- sbt: `build.sbt` or `project/build.properties`
- Go: `go.mod` (see [Go applications](#go-applications))

If more than one is present, you choose. Init writes a local **`.release`** file and offers to add it to `.gitignore`. To share defaults with collaborators, opt in to a committed `release.toml`; a local `.release` still overrides it.

Re-run detection with `release --init --force`. `--init` never creates a release.

### Gradle version location

Put the project version in `gradle.properties` (`version=…`) or as a top-level `version = "…"` / `version.set("…")` in the root `build.gradle` / `build.gradle.kts`. Another file works too if you set `version_file` in `.release`.

### Go applications

A Go project keeps its version in a plain `VERSION` file at the repo root (`version_file` in `.release` can point elsewhere): one line, `X.Y.Z`, `X.Y.Z-rcN`, with or without `-SNAPSHOT`, and nothing else. Anything else in the file is refused, and the message shows the line that was read.

`release --init` in a folder with `go.mod` writes `tool = "go"`, takes the artifact from the last element of the module path (`github.com/acme/localci` is `localci`; a trailing `/v2` is skipped), sets `tag_format = ["v{version}"]` and, after asking, creates `VERSION` with `0.0.0-SNAPSHOT` when it is missing, so the first `release -rc` is `v0.1.0-rc0`. It also offers `go test ./...` as a `before` hook. Commit `VERSION` before the first release.

`-rc`, `-fv`, `--dry-run` and `hotfix` work as for the other tools and create only `v` tags (`v0.1.0-rc0`, `v0.1.0`), the form `go install ...@v0.1.0` and the Go module proxy see. The release itself does not build anything: the [`go-build`](#go-build) connector does.

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

### Tag names

By default a release creates two tags: `VERSION` and `{artifact}-{VERSION}`. `tag_format` in `.release` or `release.toml` changes that: a list of templates with `{version}` (required, once) and `{artifact}` (optional).

```toml
tag_format = ["v{version}"]     # only v2.75.0-rc1, v2.75.0 (what Go modules need)
```

- Every template becomes a tag, created and pushed together in one atomic push. Unset keeps today's two tags.
- The **first** template is the primary tag: `release notes`, `release history`, `release status` and the saved connector runs use it, and so does the tag connectors receive (`tag` in the request). `release_version` stays the bare version, without any prefix.
- Commands that take a tag (`connectors run|status`, `undo`, `hotfix`, `notes`) accept any form in the list. A `v` is understood only when a template has it: with `["v{version}"]`, `2.75.0-rc1` is not a tag.
- Invalid lists (empty, no `{version}`, `{version}` twice, repeated templates, unknown `{placeholders}`, characters git rejects) are refused when the config loads, with the reason.
- Changing `tag_format` in a repo that already has releases: history and notes follow the primary tag only, so the older tags no longer show up there.

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
release connector add go-build                                                              # built in
release connector add homebrew-tap                                                          # built in
release connector add jira                                                                  # built in
release connector add npm-publish                                                           # built in
release connector add sign                                                                  # built in
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

When `go-build` ran before it in the chain (it must come first in the order), the release also gets the build's files: the archives, `checksums.txt` and the installer, so people can download and verify them; when [`sign`](#sign) ran, also the signature, the provenance and the public key it added. (The Homebrew formula is not a release asset: it is committed to your tap repository by `homebrew-tap`, which runs after.) The npm package is not uploaded. Each file is one `gh release upload --clobber`; on a resume the release is reused and only the files that are missing (or whose content differs: each file is compared by its sha256, never by size) are uploaded again. Without `go-build` nothing changes. `release --dry-run` calls no `gh`: it prints the assets it would upload.

### go-build

Optional, built in: `release connector add go-build`. For Go apps it cross-compiles the release with plain `go build` (no GoReleaser) after the push, so the binaries always come from the tagged commit. It builds in a temporary `git worktree`, so uncommitted changes never get in, and removes it afterwards. It skips repositories without a `go.mod` at the root. `go` must be on `PATH`.

```toml
# .release
[connectors.go-build]
main = "./cmd/hello"                   # package to build (default ".")
binary = "hello"                       # default: artifact
targets = ["darwin/amd64", "darwin/arm64", "linux/amd64", "linux/arm64"]   # default
ldflags = "-s -w -X main.version={version}"   # default; {version} has no "v"
cgo = false                            # CGO_ENABLED=0 (default)
archive_files = ["LICENSE", "README.md"]      # added next to the binary when present
```

Every build uses `-trimpath` and `GOFLAGS=-mod=readonly`. The output, in `dist/<version>/` (`dist/` must be git-ignored; the connector refuses otherwise), is one `<binary>_<version>_<os>_<arch>.tar.gz` per target, `checksums.txt` (`shasum -a 256 -c checksums.txt` verifies it) and `manifest.json`, which lists every file with its sha256 and size for the connectors that publish them. If any target fails to compile, nothing is written and the connector fails with the compiler output.

`release --dry-run` runs it too, building HEAD into a temporary folder it prints (`DRY-RUN DIR`) and publishing nothing, so the artifacts can be checked before the tag exists.

When the remote is on github.com it also renders `install.sh` next to the archives (listed in the manifest as kind `installer`), so users can install a release with `curl -fsSL <release url>/install.sh | sh`. The script is POSIX `sh`: it detects Linux or macOS and amd64 or arm64, downloads the archive and `checksums.txt` from the release, checks the archive's sha256 (`sha256sum` or `shasum -a 256`) and refuses to install on a mismatch. `VERSION` (default: the release the script came from) and `PREFIX` (default `/usr/local`, the binary goes to `PREFIX/bin`) can be set in the environment: `curl -fsSL <release url>/install.sh | PREFIX=$HOME/.local sh`.

### npm-publish

Optional, built in: `release connector add npm-publish`, after `go-build` in the order. It publishes the app's npm package (for example a wrapper that downloads the binary) with **the same version as the release**, without touching the tracked `package.json`: the release commit is extracted whole into a temp dir (never the working tree), the `version` of the package folder is set there, `npm ci` runs where the lockfile is (the repo root when it has a `package-lock.json`, so workspaces get their dev dependencies; else the package folder; with none, nothing is installed), so a `prepack` build such as `tsc` works, `npm pack` (which runs `prepack`) writes `<name>-<version>.tgz` into the `go-build` output (`@scope/name` becomes `scope-name`), and `npm publish <tgz>` uploads it. It runs with `--ignore-scripts` and without the npm token, and neither it nor `npm pack` (which runs `prepack`) sees any `RELEASE_SECRET_*` variable, so dependency code cannot read the publish token. `npm` must be on `PATH`.

```toml
# .release
[connectors.npm-publish]
package_dir = "packages/hello-js"      # folder with the package.json, relative to the repo root
access = "public"                      # or "restricted" (default "public")
rc_dist_tag = "next"                   # RCs go under this tag, finals under "latest" (default "next")
# token_secret = "npm-token"           # secret name; see below
# registry = "https://registry.npmjs.org/"
```

Release candidates (`0.1.0-rc0`) are published under `next`, so `npm install` keeps resolving the last final; finals under `latest`. Authentication: set the token once with `release secret set npm-token` (it is written to a temporary `.npmrc` for the one command and never logged), or leave it unset to use your own `npm login`. If you name a `token_secret` that is not set, the connector fails with the `release secret set <name>` command to run. If the version is already on the registry (a resumed run), the connector succeeds with `published: false` and uploads nothing.

`release --dry-run` runs it too: it packs the tarball into the dry-run folder and runs `npm publish --dry-run`, so nothing reaches the registry.

### homebrew-tap

Optional, built in: `release connector add homebrew-tap`, placed after `go-build` (and `github-release`) in the chain. For a final release it renders a Homebrew formula from the `go-build` manifest (the url and sha256 of every darwin and linux archive, `bin.install`, and a `test do` that runs `<binary> version`) and commits it to `Formula/<binary>.rb` in your tap repository with `gh api`, so `brew install owner/tap/<binary>` works. The commit is signed with the released repository's git `user.name` and `user.email` (set a noreply address there to keep your email out of a public tap); without them GitHub uses the token account's primary email. Release candidates are skipped and say so, and so is a real run where `github-release` did not publish the archives (declined, disabled or failed), because the formula would point at urls that do not exist. The formula lives only in the tap repository (and in the dry-run folder), not on the GitHub Release. It skips repositories whose `origin` is not on github.com, and a resume does not commit again when the tap already holds the same formula.

```toml
# .release
[connectors.homebrew-tap]
tap = "acme/homebrew-tap"              # owner/repo of the tap
token_secret = "homebrew-tap-token"    # default; a token that can write to the tap (contents: read and write)
desc = "Hello command line tool"       # optional; the formula's description
```

Store the token once with `release secret set homebrew-tap-token`; the [GitHub CLI](https://cli.github.com/) (`gh`) must be on `PATH`. `release --dry-run` renders the formula into the dry-run folder (`<binary>.rb`) and commits nothing.

### sign

Optional, built in: `release connector add sign`, placed after `go-build` and before `github-release` in the chain. It signs the release with a key you hold, on a laptop or in a local CI, with no hosted CI and no identity published: it needs [`cosign`](https://docs.sigstore.dev/cosign/system_config/installation/) 3.x on `PATH` (`brew install cosign`) and sends nothing to a transparency log.

It adds three files next to the build, and `github-release` uploads them:

| File | What it is |
|---|---|
| `checksums.txt.sig` | cosign signature bundle of `checksums.txt` (which lists the sha256 of every archive) |
| `<binary>_<version>.intoto.jsonl` | signed in-toto statement with SLSA provenance (v1): every archive and its sha256, the tag, the commit, the repository, the build flags, and as the builder release-cli and its version (or your own `builder_id`, below) |
| `cosign.pub` | the public key, also committed in your repo |

```toml
# .release
[connectors.sign]
public_key = "cosign.pub"              # default; committed in the app repo, the pair of the private key below
# key_secret = "cosign-key"            # default secret names
# password_secret = "cosign-password"
# builder_id = "https://github.com/acme/app"   # optional https:// URL, see below
```

By default the provenance names release-cli (its repository URL and version) as the builder. Set `builder_id` when your release files must not name the tool: `runDetails.builder.id` becomes that URL, `buildDefinition.buildType` becomes `<builder_id>/go-build/v1`, and the builder version is left out (it is optional in SLSA v1), so the tool's name appears nowhere in the statement. Anything that is not an `https://` URL is refused. Without it the statement is unchanged.

Set it up once:

```sh
cosign generate-key-pair                       # asks for a password; writes cosign.key (private, keep it out of the repo) and cosign.pub
git add cosign.pub && git commit -m "Add the release public key"
base64 < cosign.key | tr -d '\n'               # copy this one line
release secret set cosign-key                  # paste it
release secret set cosign-password             # the password you chose
```

`cosign-key` holds the key file's content (or its base64, as above, because `release secret set` reads one line). In a local CI set `RELEASE_SECRET_COSIGN_KEY` and `RELEASE_SECRET_COSIGN_PASSWORD` instead. The key is written to a private temporary file only while cosign runs, then deleted, and it is never printed or kept in the run state. After signing, `sign` checks its own output with `cosign.pub`, so a public key that is not the pair of the private key fails the release step instead of shipping files nobody can verify. A missing `cosign` or secret fails with the command that fixes it. The repository named in the provenance is the `origin` remote as an `https://` URL (SSH remotes are converted); if no repository can be derived from it, `sign` fails instead of signing an empty source. Before signing, every archive is hashed again and compared with `go-build`'s manifest and `checksums.txt`: if a file changed since `go-build`, `sign` fails naming it and signs nothing (run `go-build` again). `release --dry-run` signs with a throwaway key generated in the dry-run folder, so the whole chain is checked without the real key.

**Verify a release** (anyone, with the files from the GitHub Release and `cosign.pub`):

```sh
# the checksums are the maintainer's
cosign verify-blob --key cosign.pub --insecure-ignore-tlog --bundle checksums.txt.sig checksums.txt
# the archive is the one built from the tagged commit (repeat for each archive you use)
cosign verify-blob-attestation --key cosign.pub --insecure-ignore-tlog --type slsaprovenance1 \
  --bundle hello_1.2.3.intoto.jsonl hello_1.2.3_linux_amd64.tar.gz
```

The signature covers `checksums.txt` and the attestation covers each archive's own sha256, so both commands pass only for files that were not changed. `--insecure-ignore-tlog` is required because nothing is in a transparency log: the signature and the key are still checked. The statement is base64 JSON in `dsseEnvelope.payload` and names the tag, the commit and every archive's sha256. npm provenance and GitHub attestations need a hosted CI's identity token, so they are not part of this connector.

### jira

Optional, built in: `release connector add jira` (it stops the chain when it fails). The deploy platform refuses a prd deploy unless Jira already has the version `{artifact}-{X.Y.Z}` (for example `example-app-1.0.0`) released with its tickets delivered, so **`jira` must come before the deploy connector**: build, `jira`, deploy, `github-release`, `slack-notify`, `cursor-review`. `connector add jira` offers the position just before the first connector whose name contains `deploy`; check it with `release connector ls`. A project `order` in `.release` runs the connectors it leaves out after the ones it lists, so list `jira` there too. On a final, a chain where `jira` comes after the deploy connector (`platform-deploy`, or `deploy_connector` under `[connectors.jira]`) does not start: fix the order, then `release connectors run <tag>`. Jira Cloud only (REST v3, email and API token).

Setup, once per machine: in `~/.config/release/release.toml` add `[jira]` with `base_url = "https://your-site.atlassian.net"` and `email = "you@example.com"`, then store the token with `release secret set jira-token`. Without them an RC skips the connector and says so; a final stops the chain before the deploy.

The tickets are the keys (`ABC-123`) found in the commits since the previous tag (for a final, since the previous final), the same range as `release notes`. They are shown as a checklist with their summary and status: every key of the project is ticked, other projects' keys are listed unticked. Enter accepts, `2 5` toggles, `+ABC-9` adds, `-ABC-9` removes. The first time in a project it also asks `project_key` and `delivered_transition` (the workflow transition to the delivered status, offered from what Jira allows) and saves them under `[connectors.jira]`; `qa_statuses` there (default `Code Review`, `Code Review Approved`) lists the statuses an RC expects.

- **RC:** writes nothing and never blocks. Tickets whose status is not in `qa_statuses` are warned about.
- **Final:** creates the Jira version if it is missing (an existing one is never renamed), sets it as fixVersion of every ticket, moves each ticket that is not yet delivered, then releases the version. No tickets, or a ticket that cannot be delivered, fails the connector: the version stays unreleased and the deploy does not run. Fix the ticket in Jira (or untick it) and run `release connectors run <tag> --from jira`; tickets already delivered are left alone.

The result passed on as `prior["jira"]` is `{"mode", "jira_version", "released", "url", "issues": [{"key", "summary", "from", "to", "status", "reason"}]}`, with `status` one of `delivered`, `already`, `failed`, `warned`, `ok`.

### slack-notify

Optional, built in: `release connector add slack-notify`. After the deploy reaches `SUCCESS` (right after the push when the chain has no deploy connector) it posts one message to a Slack channel through an [incoming webhook](https://api.slack.com/messaging/webhooks): repo, version, environment, deploy type, a link to the GitHub Release, the released Jira version with its delivered tickets, and the release notes (trimmed to Slack's limits). Put it after the deploy and `github-release` in the order so it can read their results.

The webhook URL is a secret: store it with `release secret set slack-webhook` (or the env var `RELEASE_SECRET_SLACK_WEBHOOK`). It is never written to `.release`, the run state, or the logs. The first time per project it asks:

- `webhook_secret`: the secret's *name* (default `slack-webhook`).
- `notify_on`: `final` (default; release candidates are not announced) or `every-deploy`.

Then, once per release, "Send the Slack notice?" (default yes). A missing secret fails the connector with the `release secret set <name>` command to run; resume with `release connectors run <tag> --from slack-notify`. The webhook must be `https`. A deploy connector named something other than `platform-deploy` is set with `deploy_connector` under `[connectors.slack-notify]`.

### Writing a connector

A connector is a `release.connectors` entry point pointing at a zero-arg callable. release-cli runs it in a subprocess with the action (`questions` or `run`) as `argv[1]`, one JSON request on stdin, and expects one JSON object on stdout. Logs go to stderr and are shown live.

- Request: `protocol`, `action`, `connector`, `repo`, `remote_url`, `repo_root`, `artifact`, `release_version`, `tag` (the primary tag), `tags`, `tag_format`, `sha`, `mode` (`rc`/`fv`), `dry_run`, `prior` (`{name: {status, result, error, job_id}}`), `config`, `resume_job_id`, `out_dir`; `run` adds `answers`.
- `questions` → `{"protocol": 1, "questions": [...], "applies": true, "timeout_seconds": 1860}`. Not for this project: `"applies": false, "reason": "..."` (skipped, exit 0). A prerequisite that **already finished** unsuccessfully (e.g. a resumed build that is not `SUCCESS`): `"applies": false, "blocked": true, "reason": "..."` (recorded `failed` with that reason, exit 1). The CLI asks every connector before any `run`, so `prior` in `questions` does not include connectors that will run in this same invocation. If that result is simply not there yet, return the questions; `run` receives the real `prior` and is where a missing or failed prerequisite is refused. Question types `bool`, `choice`, `text`, `int`, `multi`, with `default`, `default_when`, `when`, `persist`, `pattern` (text), `min`/`max` (int). 30 s timeout.
- `multi` asks "pick from this list": `{"id": "issues", "type": "multi", "prompt": "...", "options": [{"value": "ABC-1", "label": "ABC-1 Fix login"}], "default": ["ABC-1"], "other": {"pattern": "[A-Z][A-Z0-9_]+-[0-9]+"}}`. `options` may be plain strings; `default` is a subset of the values (none if omitted); `other` (optional) lets the user add extra values matching `pattern`; `persist` must stay `none`. The answer in `run` is a list of strings: the picked options in option order, then the extras. In the terminal the user sees a numbered `[x]`/`[ ]` list: Enter accepts, `2 5` toggles, `+KEY` adds, `-KEY` removes. `--defaults` takes `default`.
- `"dry_run": "safe"` in the `questions` answer says the connector can run under `release --dry-run`: it builds or renders into `out_dir` and changes nothing outside it (no upload, no publish, no push; a publishing connector only reports what it would send). Without it, a dry run only lists the questions, as before. See [Dry run with connectors](#dry-run-with-connectors).
- `run` → `{"protocol": 1, "ok": true, "result": {...}}`, optionally `status` (`interrupted`), `error`, `job_id`.
- Any other `protocol`, invalid JSON, missing fields, or a non-zero exit from `run` marks the connector `failed` with the reason. On timeout or Ctrl-C the CLI sends SIGINT and waits 10 s for an `interrupted` answer carrying the `job_id`.

#### Dry run with connectors

`release --dry-run` asks every connector as usual. Each one that answered `"dry_run": "safe"` is then run, in `order`, with `dry_run: true` and `out_dir` set to `<tmp>/release-dry-run/<repo>/<version>/` (emptied first, printed as `DRY-RUN DIR`). Its result reaches the next connector in `prior`, like a real run. The request has `sha` = `HEAD` and `tags` = the tags the release would create, which are not on `origin` yet, so a safe connector must not read the pushed tag. Questions are asked the way a real run asks them (`--defaults` takes each default) but nothing is stored in the project config; the other connectors still only ask.

Nothing is tagged or pushed and no run state is saved. A safe connector that fails makes the dry run exit 1 and stops the rest of the chain if it has `break_on_error`. With `--json` the object gains `dry_run_dir` (or `null` when no connector ran) and `connectors` (name → `{status, result, error, job_id}` of the safe ones that ran). `out_dir` is `null` in every request that is not a dry-run `run`.

The built-in [`release_cli/cursor_review.py`](release_cli/cursor_review.py) is a small complete example; [`release_cli/connectors.py`](release_cli/connectors.py) has the exact validation.

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

`release hotfix` refuses when the tag is missing (it fetches it from `origin` if only the remote has it), is a release candidate, when the working tree is dirty, when `hotfix/X.Y.Z+1` already exists locally or on `origin`, when `X.Y.Z+1` (or one of its RCs) is already tagged, or when the version file at that tag does not read `X.Y.Z`. In every case nothing is created. It works for Maven (including multi-module), Gradle and sbt.

The first release on the branch pushes it to `origin`. After `-fv`, `release` reminds you to merge the branch back and does not do it. Expect a conflict in the version file: keep the main branch's version.

## Status, undo, notes

```sh
release status              # version, open RC, last tag, branch vs origin, the last tag's connectors
release status --json       # the same as one JSON object (--no-fetch skips the fetch)
release history             # releases of this repo, newest first, with what each connector did
release notes [tag]         # markdown notes since the previous tag (default: the latest tag)
release undo 2.75.0-rc1     # deletes that RC's tags on origin and locally, after asking (--force skips)
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
