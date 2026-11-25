# Rust (Cargo) applications: roadmap

## Goal
A Rust application gets the same `release -rc` / `release -fv` flow as a Go app (see
[go-apps](../go-apps/ROADMAP.md)): the version lives in `Cargo.toml`, a `cargo-build` connector builds the pushed tag
for every configured target on the maintainer's machine, including a Windows `.exe` cross-compiled from macOS, and
the go-apps connectors (`sign`, `github-release`) sign and publish it. The first consumer is a small Windows desktop
app (Cargo workspace, eframe/egui with the glow backend, one binary); the design stays generic.

- **Hypothesis:** pending, in the user's words.
- **Definition of done:** pending, in the user's words.

## What exists today (read 2026-11-24, after merging main)
- go-apps is merged (phases 1–4; phase 5 and T-10 dropped, CI is local): `tag_format` (`tagformat.py`), dry-run runs
  connectors that declare `"dry_run": "safe"`, `go-build` (`go_build.py`: worktree at the tag, reproducible tar.gz,
  `checksums.txt`, `manifest.json`, `install.sh`), `github-release` uploads every manifest entry except `kind: npm`,
  `sign` (cosign over `checksums.txt`, SLSA provenance whose subjects are the `kind: archive` entries, configurable
  builder id), `homebrew-tap`, `npm-publish`.
- The packaging helpers are private to `go_build.py`; `sign.py` checks and attests only `kind: archive`.
- `detect.py` lists `Cargo.toml` as an unsupported hint. Tools: `maven`, `gradle`, `sbt`, `go` (`config.py`).
- Zero runtime dependencies, Python ≥ 3.11 (`tomllib` for reading; writes are text edits).
- The first consumer already builds its `.exe` on a hosted Windows runner; this plan does not depend on that.

## Decisions
- **Build locally, not in hosted CI** (user, 2026-11-22). The connector builds the pushed tag where `release` runs.
- **Cross-compiling a GUI app to Windows from macOS is unproven, so phase 1 has a spike** (T-01):
  `x86_64-pc-windows-msvc` with `cargo-xwin` first, `x86_64-pc-windows-gnu` with mingw-w64 as the fallback. The user
  checks on their own Windows PC that the exe starts and shows its window (user, 2026-11-24). If neither works, stop
  and go back to the user before T-04. T-04 implements only the path(s) the spike proved.
- **`cargo` tool adapter; the version stays in `Cargo.toml`** (no `VERSION` file; `CARGO_PKG_VERSION` stamps the
  binary, so no ldflags). Source: `[workspace.package] version` when present, else the root `[package] version`.
  Members with `version.workspace = true` follow it; a member with its own literal version makes `read` fail and
  names it (one version per repository).
- **`Cargo.lock`, when committed:** the adapter rewrites the `version` of each workspace member's `[[package]]` entry
  (path packages: no `source` line) with a text edit in the same commit, so `--locked` builds pass. No `cargo` call.
- **Version format unchanged** (`X.Y.Z-rcN`, `-SNAPSHOT`, `X.Y.Z-rcN-SNAPSHOT`): all valid SemVer.
- **Tags:** default `tag_format` for `cargo` is `["v{version}"]`, as for `go`.
- **Packaging is shared, not copied:** the archive, checksum, manifest and worktree helpers move out of
  `go_build.py` into one module both builders use (T-03); `tests/test_go_build.py` passes unchanged.
- **Windows targets ship a reproducible `.zip` and the bare `.exe`** (user, 2026-11-24: yes to the exe). Other OSes
  ship tar.gz only. The exe is a new manifest kind, `binary`; `sign` lists it in `checksums.txt`, disk-checks it and
  makes it a provenance subject, since it is what people download (T-03). This changes shared code that go-apps
  built; it is a cargo-rust task, not a go-apps one.
- **`install.sh` only when a darwin or linux target is built** (useless for a Windows-only release).
- **Targets `cargo-build` accepts:** the host triple, the other arch of the host OS on macOS
  (`aarch64`/`x86_64-apple-darwin`), and Windows triples. Linux from macOS needs a cross linker and is out; such a
  target fails before anything builds, naming the reason.
- **Toolchain checks in `run`, before any build, and in `release doctor`:** `cargo`, each target in
  `rustup target list --installed`, `cargo-xwin` (msvc off Windows) or `x86_64-w64-mingw32-gcc` (gnu). A missing
  piece fails with the exact install command. The connector never accepts a license on the user's behalf.
- **Signing:** the go-apps `sign` connector as extended by T-03. Windows Authenticode (what SmartScreen checks) is not
  in this plan (open question).
- **App-side, not release-cli:** Windows icon and manifest, `windows_subsystem = "windows"`, installer.
- **Out:** `cargo publish` to crates.io, MSI or other installers, hosted-CI builds, Linux cross-builds from macOS.
- **Public repo:** plan, code and fixtures name no private app, repo or path; fixtures use `acme/*`. The consumer's
  acceptance task lives in the consumer's own plan (as go-apps T-09 did).

## Contracts
**Project config**, Rust app:
```toml
tool = "cargo"
artifact = "acme-app"            # asked by `release init` for a virtual workspace (no root [package])
tag_format = ["v{version}"]

[connectors.cargo-build]
package = "app"                  # -p; default: the root package
binary = "acme-app"              # --bin; default: artifact
targets = ["x86_64-pc-windows-msvc"]   # default: the host triple only
archive_files = ["LICENSE", "README.md"]
```
Build per target: `cargo build --release --locked -p <package> --bin <binary> --target <triple>`; for
`*-windows-msvc` off Windows, `cargo xwin build` with the same flags. The linker follows from the triple (msvc → xwin,
gnu → mingw); there is no linker key. The project's `[profile.release]` is used as is.

**Layout and manifest:** go-apps' (`dist/<version>/`, `checksums.txt`, `manifest.json` with
`artifacts: [{name, kind, os, arch, sha256, size}]`), plus:
- Windows archive `<binary>_<version>_windows_<arch>.zip` (`kind: archive`, holds `<binary>.exe` and the
  `archive_files`), bare exe `<binary>_<version>_windows_<arch>.exe` (`kind: binary`, with `os` and `arch`).
- Other OSes: `<binary>_<version>_<os>_<arch>.tar.gz`, as go-build.
- `checksums.txt` lists every `archive` and `binary`. The `build` object (it feeds provenance):
  `{package, binary, targets, profile: "release", flags: ["--release", "--locked"], rustc: "<rustc -V>"}`.

Triple → `os`/`arch`: `x86_64-pc-windows-*` → `windows/amd64`, `aarch64-pc-windows-msvc` → `windows/arm64`,
`aarch64-apple-darwin` → `darwin/arm64`, `x86_64-apple-darwin` → `darwin/amd64`, `x86_64-unknown-linux-gnu` →
`linux/amd64`, `aarch64-unknown-linux-gnu` → `linux/arm64`.

**Order:** `cargo-build`, `sign`, `github-release`, then `slack-notify` / `cursor-review`.

## Phases and tasks
### Phase 1: Rust projects tag, shared packaging, the cross-compile spike (shippable: `release -rc` tags a Rust app)
Release PR: — · last reviewed: — · last signed off: — · open: —
| Task | Repo | Depends on | Status | PR | Note |
|---|---|---|---|---|---|
| [T-01](tasks/T-01.md) Spike: Windows GUI exe cross-compiled on macOS (xwin, then mingw) | release-cli | — | todo | | gate: the user runs the exe on their Windows PC |
| [T-02](tasks/T-02.md) `cargo` tool: detection, `Cargo.toml` + `Cargo.lock` versions | release-cli | — | todo | | parallel |
| [T-03](tasks/T-03.md) Shared packaging: zip, bare binary, `sign` covers binaries | release-cli | — | todo | | parallel |

### Phase 2: build connector
Release PR: — · last reviewed: — · last signed off: — · open: —
| Task | Repo | Depends on | Status | PR | Note |
|---|---|---|---|---|---|
| [T-04](tasks/T-04.md) `cargo-build` connector and Rust fixture | release-cli | P1 (T-01 gate passed) | todo | | |

### Phase 3: first consumer
| Task | Repo | Depends on | Status | PR | Note |
|---|---|---|---|---|---|
| T-05 The Windows desktop app releases with release-cli (acceptance) | consumer app | P2 | todo | | in the consumer's own plan; not named here |

## Open questions
- Windows Authenticode signing (SmartScreen warning on an unsigned exe): certificate, cost, where the key lives — user.
- Should the first consumer drop its hosted Windows build job once local releases work, or keep it for PR checks? —
  consumer's own plan.

## Handoff

## Log
- 2026-11-22: requirement written from the first consumer's setup (Cargo workspace, edition 2024, eframe/glow, one
  Windows binary). Rebased on go-apps: local build connector (user's choice), shared manifest, `sign` and
  `github-release` assets. Task files not written yet.
- 2026-11-24: feature intake. main merged (go-apps done). User: attach the bare exe; the spike is checked on the
  user's own Windows PC. Packaging extracted from go-build (T-03), `sign` extended to `binary`; no linker key; task
  files T-01–T-04 written.
- 2026-11-24: approved by the user; plan put on main through a docs-only PR. Hypothesis and definition of done still
  pending in the user's words; the consumer's repo is named in its own plan.
