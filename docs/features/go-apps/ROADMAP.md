# Go applications: roadmap

## Goal
A Go application gets the same `release -rc` / `release -fv` flow as a Maven, Gradle or sbt project: `v` tags,
cross-compiled binaries with the version stamped in, archives and checksums on the GitHub Release, an npm package
published in lockstep, a Homebrew formula and an install script. `release --dry-run` builds every artifact into a
temporary folder and publishes nothing, so a release can be checked before it is cut. The first consumer is
localci (one Go binary with embedded templates, static files and migrations, plus a TypeScript Playwright package
under `packages/playwright`); the design stays generic for any Go app.

- **Hypothesis:** delegated, to confirm. Draft: one tool releases every app; Go apps stop needing a second release
  system, and a dry-run proves the artifacts before the tag exists.
- **Definition of done:** delegated, to confirm. Draft: a dry-run release of localci produces the artifacts listed in
  Contracts → Layout, and a real `-rc` publishes them with nothing done by hand.

## What exists today (read 2026-11-22, release-cli 0.3.0)
- release-cli versions and tags; it builds nothing for any language. It writes a version file, makes two
  `[manual-release]` commits, tags `X.Y.Z` and `{artifact}-X.Y.Z`, pushes atomically, then runs connectors.
- Building and publishing happen in **hooks** (shell, before the push, can roll back) or **connectors** (after the
  push, protocol 1, results passed on as `prior`). `github-release` creates the release with notes only, no assets.
- No signing or provenance anywhere. `--dry-run` writes nothing and runs no hooks or connectors.
- `detect.py` already lists `go.mod` as an unsupported hint. CI (`test.yml`) runs ruff and pytest, no Go.

## Decisions
- **Build and publish are built-in connectors** (the user's direction): `go-build`, then `github-release` (now with
  assets), `npm-publish`, `homebrew-tap`. They update with `release update`, like `jira` and `slack-notify`. A
  connector builds the pushed tag, so artifacts always match a tag on origin. Hooks stay for tests (`go test ./...`).
- **Native `go build`, not a GoReleaser wrapper** (user: "whichever is better"; 2026-11-22). The matrix build,
  tar.gz, sha256 and a manifest are small, testable in CI with only `go`, and need no second tool on every machine.
- **A `VERSION` file holds the version** (user, 2026-11-22). Plain text at the repo root (path set by
  `version_file`), e.g. `0.2.0-SNAPSHOT`; the `go` tool reads and writes it like the other adapters, so the two
  `[manual-release]` commits, rollback and hotfix work unchanged. The binary still gets the version through ldflags.
- **Tag format is configurable (`tag_format`), default `["v{version}"]` for Go.** `go install …@vX.Y.Z` and the Go
  module proxy only see `v` tags. Existing tools keep `["{version}", "{artifact}-{version}"]`.
- **RCs:** `vX.Y.Z-rcN` (valid semver). npm publishes RCs under dist-tag `next`, finals under `latest`. Homebrew only
  for finals. Known nuance: semver orders `rc10` before `rc9`; accepted (document it), *open if not*.
- **Dry-run builds.** A connector may declare `"dry_run": "safe"` in its `questions` answer; `release --dry-run` then
  runs it with `dry_run: true` into a temp folder. Publishing connectors, when safe, only report what they would
  upload (`npm publish --dry-run`, the rendered formula). Others stay as today: questions only.
- **Provenance before launch, signed locally** (user, 2026-11-22: before launch, in the local CI; GitHub Actions has
  no quota until next month). GitHub attestations and npm `--provenance` need a hosted CI's identity token, which a
  local run lacks (*to verify in T-08*). So phase 3 signs with a maintainer key: a `sign` connector (cosign, key-based,
  no identity published) signs `checksums.txt` and attaches an in-toto SLSA provenance statement for every
  archive. It runs in the same chain, on a laptop or in the local CI. The npm package ships without provenance at
  launch. CI-identity provenance (attestations, npm provenance) is phase 5, once Actions has quota.
- **`go install` path:** ldflags do not apply, so the binary reads its version from `runtime/debug.ReadBuildInfo`
  when the stamped one is `dev` (app-side, in localci's adoption task).
- **Public repo:** plan, code, tests and fixtures name no private company, repo, host or path; fixtures use `acme/*`.

## Contracts
**Project config** (`.release` / `release.toml`), Go app:
```toml
tool = "go"
artifact = "localci"
version_file = "VERSION"         # one line: X.Y.Z[-rcN][-SNAPSHOT]
tag_format = ["v{version}"]

[connectors.go-build]
main = "./cmd/localci"           # package to build
binary = "localci"               # default: artifact
targets = ["darwin/amd64", "darwin/arm64", "linux/amd64", "linux/arm64"]   # default
ldflags = "-s -w -X main.version={version}"                                # {version} has no v
cgo = false                      # CGO_ENABLED=0 (default)
archive_files = ["LICENSE", "README.md"]   # added next to the binary when present

[connectors.npm-publish]
package_dir = "packages/playwright"
access = "public"
rc_dist_tag = "next"

[connectors.homebrew-tap]
tap = "acme/homebrew-tap"        # owner/repo, formula at Formula/<binary>.rb
token_secret = "homebrew-tap-token"
```
Build flags are fixed: `-trimpath`, `GOOS`/`GOARCH` per target, `GOFLAGS=-mod=readonly`.

**Layout** (real run: `dist/<version>/` in the repo, which must be git-ignored; dry-run: a temp dir printed as
`dry_run_dir`). `<version>` has no `v`.
```
<binary>_<version>_<os>_<arch>.tar.gz    # one per target: <binary>, plus archive_files
checksums.txt                            # "<sha256>  <file>" per line, sorted by file name
<npm-name>-<version>.tgz                 # npm pack, when npm-publish is enabled
<binary>.rb                              # Homebrew formula (finals): lives in the tap repo, not on the GitHub Release; also in the dry-run folder
install.sh                               # installer: detects os/arch, downloads, verifies checksums.txt
manifest.json
checksums.txt.sig                        # phase 3: cosign signature bundle of checksums.txt
<binary>_<version>.intoto.jsonl          # phase 3: signed SLSA provenance statement covering every archive
cosign.pub                               # phase 3: the public key, also committed in the app repo
```
`manifest.json` = the `go-build` result in `prior`:
`{"app", "version", "tag", "commit", "dir", "artifacts": [{"name", "kind": "archive|checksums|npm|formula|installer", "os"?, "arch"?, "sha256", "size"}]}`.
Results: `github-release` adds `"assets": [names]`; `npm-publish` `{"name", "version", "dist_tag", "published"}`;
`homebrew-tap` `{"formula", "commit"?, "skipped_reason"?}`.

**Order:** `go-build`, `sign` (phase 3), `github-release`, `npm-publish`, `homebrew-tap`, then `slack-notify` / `cursor-review`.
**Secrets:** `npm-token` (or the user's `npm login`), `homebrew-tap-token`, `cosign-key` and `cosign-password` (phase 3); names in config, values via `release secret`.

## Phases and tasks
<!-- Release PR: release/go-apps-p<n> → main; last reviewed commit: 85e66a3 (2026-11-22, 6 findings: 0 critical, 0 high, 1 medium, 5 low) -->
**Phase 1 release:** merged 2026-11-22 via [#20](https://github.com/GutiNicolas/release-cli/pull/20) (merge commit fe3feee); `release/go-apps-p1` deleted.
### Phase 1: Go projects and `v` tags (shippable alone: `release -rc` tags a Go app)
| Task | Repo | Depends on | Status | PR | Note |
|---|---|---|---|---|---|
| [T-01](tasks/T-01.md) Configurable tag format (`v` tags) | release-cli | — | merged | [#19](https://github.com/GutiNicolas/release-cli/pull/19) | |
| [T-02](tasks/T-02.md) `go` tool: detection and `VERSION` file | release-cli | T-01 | merged | [#21](https://github.com/GutiNicolas/release-cli/pull/21) | |

### Phase 2: build and publish connectors, dry-run that builds
**Phase 2 release:** merged 2026-11-23 via [#23](https://github.com/GutiNicolas/release-cli/pull/23) (merge commit 62612bc); last reviewed: aced88d (2026-11-23; round 1 at 888348f: 9 findings, 0 critical, 0 high, 4 medium fixed, 5 low open; round 2 over the fixes: 1 low).
| Task | Repo | Depends on | Status | PR | Note |
|---|---|---|---|---|---|
| [T-03](tasks/T-03.md) Dry-run runs connectors that declare it safe | release-cli | P1 | merged | [#22](https://github.com/GutiNicolas/release-cli/pull/22) | |
| [T-04](tasks/T-04.md) `go-build` connector and Go fixture | release-cli | T-03 | merged | [#24](https://github.com/GutiNicolas/release-cli/pull/24) | |
| [T-05](tasks/T-05.md) `github-release` uploads the manifest's assets | release-cli | T-04 | merged | [#26](https://github.com/GutiNicolas/release-cli/pull/26) | parallel with T-06, T-07 |
| [T-06](tasks/T-06.md) `npm-publish` connector (lockstep) | release-cli | T-04 | merged | [#25](https://github.com/GutiNicolas/release-cli/pull/25) | parallel |
| [T-07](tasks/T-07.md) `homebrew-tap` connector and install script | release-cli | T-04 | merged | [#27](https://github.com/GutiNicolas/release-cli/pull/27) | parallel |

### Phase 3: signed provenance, local (before localci's launch)
**Phase 3 release:** merged 2026-11-23 via [#29](https://github.com/GutiNicolas/release-cli/pull/29) (merge commit bdae463); last reviewed: 9fe985c (2026-11-23; round 1 at e644460: 6 findings, 0 critical, 0 high, 2 medium fixed, 4 low open; round 2 over the fixes: 0).
| Task | Repo | Depends on | Status | PR | Note |
|---|---|---|---|---|---|
| [T-08](tasks/T-08.md) `sign` connector: cosign signature and SLSA provenance | release-cli | P2 | merged | [#28](https://github.com/GutiNicolas/release-cli/pull/28) | |

### Phase 4: first consumer
| Task | Repo | Depends on | Status | PR | Note |
|---|---|---|---|---|---|
| [T-09](tasks/T-09.md) localci releases with release-cli (acceptance) | localci | P3, localci's T-22 | todo | | ships as localci's T-22 (its plan, its worker, base `release/localci-p4`); no separate worker. Done when T-22 merges with these criteria met |

### Phase 5: CI-identity provenance (when GitHub Actions has quota; after launch)
| Task | Repo | Depends on | Status | PR | Note |
|---|---|---|---|---|---|
| [T-10](tasks/T-10.md) `gh-workflow` connector, GitHub attestations, npm provenance | release-cli | P4 | todo | | not before Actions quota |

## Open questions
- Where the private signing key lives for the local CI (keychain on the maintainer's machine, or a local CI secret) — user, in T-09.
- npm package name and scope, tap repo name — user (localci's open question, settled in T-09).
- localci's T-22 (planned on GoReleaser) is rewritten to consume this after approval — localci's planning session.
- localci's plan asks for a configurable provenance builder id before its T-22 (the `sign` statement names
  release-cli as the builder today), and treats the tool's name as private although this repo is public — user.

## Log
- 2026-11-22: plan written (feature intake). Build and publish as connectors per the user. Not yet approved.
- 2026-11-22: user decisions: native build, `VERSION` file (T-02 reworked), naming localci here is fine. Provenance at launch still open.
- 2026-11-22: provenance before launch, in the local CI: phase 3 is now key-signed provenance (T-08 `sign`); the GitHub workflow work moved to phase 5 (T-10).
- 2026-11-22: approved by the user; plan put on main through a docs-only PR.
- 2026-11-22: phase 1 merged to main (#20): T-01, T-02 and one review fix (`fix(T-01)`: `tag_format` refuses templates git cannot tag). Phase 2 is ready to dispatch.
- 2026-11-22: phase 1 merged to main (#20): T-01, T-02 and one review fix (fix(T-01): `tag_format` refuses templates git cannot tag). Phase 2 is ready to dispatch.
- 2026-11-23: phase 2 merged to main (#23): T-03 to T-07 and four review fixes (`fix(T-06)`: npm packs `package_dir` from the release commit; `fix(T-07)`: homebrew-tap skips when github-release did not publish; `fix(T-05)`: assets compared by digest, not size; the formula is not a release asset). Open lows for later: hotfix overwriting a newer formula, archive extras collisions, `GOFLAGS` overwritten, install.sh hardening, timeouts on go/git/gh, `tarfile` `filter=` needs Python 3.11.4+. Phase 3 (T-08 `sign`) is ready to dispatch.
- 2026-11-23: phase 3 merged to main (#29): T-08 and two review fixes (`fix(T-08)`: the provenance source is an https URL for SSH remotes and never empty; archives and `checksums.txt` are checked against disk before signing). Open lows for later: key file left if `sign` is SIGKILLed, cosign inherits every `RELEASE_SECRET_*`, cosign 3.x not enforced, a failed `sign` with `break_on_error=false` lets `github-release` upload unsigned files without a warning. Not yet run: signing with the real maintainer key (phase 4). Phase 4 (T-09, localci) is ready to dispatch.
- 2026-11-23: dispatch found T-09 not ready: localci's T-22 does the same work in the same files, waits on localci's
  T-21 and wave 1, and `release/localci-p4` is not cut. The user linked them: T-09 ships through T-22, no
  separate worker; `integrate` here marks T-09 once T-22 is integrated in localci.
