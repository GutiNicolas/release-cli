# Install with Homebrew: roadmap

## Goal
Anyone on macOS or Linux installs release-cli with `brew install GutiNicolas/tap/release-cli`, without cloning the
repo or installing uv first. `release update` and `release doctor` understand that install (and a
`uv tool install git+https://...` install) instead of looking for a clone that isn't there.

- **Hypothesis:** delegated. Draft: now that the repo is public, a one-line install removes the main barrier for new
  users; the clone install stays for people who work on release-cli.
- **Definition of done:** as given in the task: on macOS (and Linux in a container), `brew install` from the tap gives
  a `release` that runs `release --version`; under brew, `release update` tells the user to run `brew upgrade` and
  `release doctor` doesn't FAIL the install check; tests cover the detection; a PR is open.

## What exists today (read 2026-11-24, release-cli 0.3.0)
- Install: clone + `install.sh` (`uv tool install <clone>`), which writes `source.toml` with the clone's path.
- `release update` finds the clone (`source.toml`, else a uv receipt that points at a local directory), pulls it and
  reinstalls with uv; with no clone it fails with "no release-cli source found".
- `release doctor` WARNs when no clone is found.
- Built-in connectors run on the interpreter running `release`; only `release connector add <url>` needs uv (it
  makes a separate environment for URL connectors).
- The public repo has no tags. The built-in `homebrew-tap` connector renders formulas from the `go-build` manifest
  (prebuilt binaries); it doesn't fit a Python package.

## Decisions
- **Homebrew only, not npm.** release-cli is a Python tool; npm would only wrap it.
- **Formula:** Python virtualenv formula (`depends_on "python@3.15"`, the version `brew info python` resolves to on
  2026-11-24; `virtualenv_install_with_resources`, no resources since `dependencies = []`). Hatchling, the build
  backend, is fetched by pip's isolated build at install time; checked working on macOS. `test do` runs
  `release --version`. The draft lives in `packaging/homebrew/release-cli.rb`; the tap holds the published copy.
- **Install channel comes from the running interpreter, checked before the clone lookup.** A user who moves from a
  clone to brew still has `source.toml`; brew must win. Homebrew: `sys.prefix` resolves under
  `Cellar/release-cli/` (a brew Python's own prefix is `Cellar/python@…`, so that isn't enough). uv: `sys.prefix` is
  the uv tool environment and the receipt's requirement isn't a local directory (a git URL or an index). A receipt
  pointing at a directory is still the clone install.
- **Under a package manager, `release update` prints the upgrade command, asks the new config keys, exits 0.** It
  never touches git or uv. Running it after `brew upgrade` is how new settings get asked (no install hook in brew).
- **uv isn't a brew dependency.** Only connectors installed from a URL need it; the built-in connectors don't. The
  error from `release connector add <url>` and doctor's uv check say `brew install uv` under brew.
- **Versions are tagged by hand for now.** release-cli has no Python/pyproject adapter; adding one (PEP 440 versions,
  no `-SNAPSHOT`) is its own feature, left as a follow-up. This PR bumps `pyproject.toml` to 0.4.0, the first version
  with the brew-aware update; 0.3.0 stays untagged.

## Release steps (by hand, per version)
1. Merge the PR into `main`; `pyproject.toml` holds the version (`X.Y.Z`).
2. Tag and push: `git tag vX.Y.Z <main sha> && git push origin vX.Y.Z`, then `gh release create vX.Y.Z --generate-notes`.
3. Hash the tarball GitHub serves:
   `curl -fsSL https://github.com/GutiNicolas/release-cli/archive/refs/tags/vX.Y.Z.tar.gz | shasum -a 256`.
4. In the tap repo, set `url` and `sha256` in `Formula/release-cli.rb` (copy of `packaging/homebrew/release-cli.rb`),
   commit with the `[release-cli]` prefix, push.
5. Check: `brew update && brew upgrade release-cli && release --version`.
6. Bump `pyproject.toml` on `main` to the next version when work on it starts.

## Phases
One phase, one PR in this repo plus the tap repo.

| Item | Status |
| --- | --- |
| Detection in `update.py` / `doctor.py`, tests | done (PR) |
| Formula draft, local-tap install on macOS | done |
| Linux (`homebrew/brew` container), same checks | done |
| README Install section | done (PR) |
| Tag `v0.4.0` on the public repo | waiting for the user's go-ahead |
| Tap repo `GutiNicolas/homebrew-tap` with `Formula/release-cli.rb` | waiting for the user's go-ahead |

## Open questions
- Automate steps 2-4 later: a pyproject adapter for `release -fv`, and a tap update from a connector or a workflow.

## Log
- 2026-11-24: plan written and built in one session; brew install, `release update` and `release doctor` checked on
  macOS and in a `homebrew/brew` Linux container, from a local tap pointing at a tarball of the branch.
