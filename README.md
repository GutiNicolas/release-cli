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

`n` skips that hook for this run. `--dry-run` prints the plan and does not write or run hooks. `--skip-hooks` skips every hook. `-y` answers yes to all hook prompts.

## Git

The working tree must be clean and the tags must not already exist. Commits and tags are pushed with a single `git push --atomic`. If an `after` hook or the push fails, version files are restored and unpushed local commits are reset.

## License

[GPL-3.0-only](LICENSE)
