# Rust (Cargo) applications: roadmap

## Goal
A Rust application gets the same `release -rc` / `release -fv` flow as a Go app (see
[go-apps](../go-apps/ROADMAP.md)): the version lives in `Cargo.toml`, a `cargo-build` connector builds the pushed tag
for every configured target on the maintainer's machine, including a Windows `.exe` cross-compiled from macOS, and
the go-apps connectors (`sign`, `github-release` with assets) sign and publish it unchanged. The first consumer is a
small Windows desktop app (Cargo workspace, eframe/egui with the glow backend, one binary); the design stays generic.

## What exists today (read 2026-11-22)
- release-cli core never builds or publishes; connectors do. `detect.py` lists `Cargo.toml` as an unsupported hint.
- go-apps (approved, on main) adds what this plan reuses: `tag_format` (T-01), dry-run runs connectors declared
  `"dry_run": "safe"` (T-03), the `go-build` layout and `manifest.json` (T-04), `github-release` uploads the
  manifest's assets (T-05), the `sign` connector (T-08).
- The first consumer already builds its `.exe` on a hosted Windows runner; this plan does not depend on that.

## Decisions
- **Build locally, not in hosted CI** (user, 2026-11-22). Same as go-apps: the connector builds the pushed tag where
  `release` runs; hosted CI quota is not needed. A connector that waits for a hosted CI run is out (go-apps phase 5
  covers workflows when there is quota).
- **Cross-compiling to Windows from macOS is unproven for a GUI app, so phase 1 starts with a spike** (T-01):
  `x86_64-pc-windows-msvc` with `cargo-xwin` first, `x86_64-pc-windows-gnu` with mingw-w64 as the fallback. The exe
  must start and show its window on Windows. If neither works, stop and go back to the user before T-03.
- **`cargo` tool adapter; the version stays in `Cargo.toml`** (no `VERSION` file: Cargo already has one, and
  `CARGO_PKG_VERSION` stamps the binary, so no ldflags). Source: `[workspace.package] version` when present, else
  `[package] version`. Members with `version.workspace = true` follow it; a member with its own literal version makes
  `read` fail and names it (one version per repository).
- **`Cargo.lock`, when committed:** the adapter rewrites the `version` of each workspace member's `[[package]]` entry
  with a plain text edit in the same commit, so `--locked` builds still pass. No `cargo` call in the adapter.
- **Version format unchanged** (`X.Y.Z-rcN`, `-SNAPSHOT`, `X.Y.Z-rcN-SNAPSHOT`): all valid SemVer, Cargo accepts them.
- **Tags:** `tag_format` from go-apps; default for `cargo` is `["v{version}"]`.
- **`cargo-build` mirrors `go-build`:** builds the pushed tag with `--release --locked` per target, writes the same
  layout and `manifest.json`, declares `"dry_run": "safe"`. Windows archives are `.zip` (tar.gz elsewhere).
- **Toolchain checks before anything runs:** in `questions`, the connector checks `cargo`, each target in
  `rustup target list --installed`, and `cargo-xwin` (msvc) or the mingw linker (gnu); a missing piece fails with the
  exact install command. `release doctor` reports the same.
- **Signing:** the go-apps `sign` connector (cosign over `checksums.txt`, SLSA provenance) applies as is. Windows
  Authenticode (what SmartScreen checks) is a different signature and is not in this plan (open question).
- **App-side, not release-cli:** Windows icon and manifest, `windows_subsystem = "windows"`, installer.
- **Out:** `cargo publish` to crates.io, MSI or other installers, hosted-CI builds.
- **Public repo:** plan, code and fixtures name no private app, repo or path; fixtures use `acme/*`.

## Contracts
**Project config**, Rust app:
```toml
tool = "cargo"
artifact = "acme-app"            # asked by `release init` for a virtual workspace (no root [package])
tag_format = ["v{version}"]

[connectors.cargo-build]
package = "app"                  # -p
binary = "acme-app"              # --bin; default: artifact
targets = ["x86_64-pc-windows-msvc"]   # default: the host triple only
windows_linker = "xwin"          # "xwin" (msvc targets) | "mingw" (gnu targets)
archive_files = ["LICENSE", "README.md"]
```
Build command per target: `cargo build --release --locked -p <package> --bin <binary> --target <triple>` (with
`cargo xwin build` for msvc targets off Windows). The project's `[profile.release]` is used as is.

**Layout and manifest:** exactly go-apps' (`dist/<version>/`, `checksums.txt`, `manifest.json` with
`artifacts: [{name, kind, os, arch, sha256, size}]`), so `sign` and `github-release` need no change. Archive name:
`<binary>_<version>_<os>_<arch>.zip|.tar.gz`. Triple → `os`/`arch`: `x86_64-pc-windows-*` → `windows/amd64`,
`aarch64-pc-windows-msvc` → `windows/arm64`, `aarch64-apple-darwin` → `darwin/arm64`, `x86_64-apple-darwin` →
`darwin/amd64`, `x86_64-unknown-linux-gnu` → `linux/amd64`, `aarch64-unknown-linux-gnu` → `linux/arm64`.

**Order:** `cargo-build`, `sign`, `github-release`, then `slack-notify` / `cursor-review`.

## Phases and tasks
<!-- Requirement only: task files are written by gutix:feature-intake before any dispatch. -->
### Phase 1: Cargo projects and the cross-compile spike
| Task | Repo | Depends on | Status | PR | Note |
|---|---|---|---|---|---|
| T-01 Spike: Windows GUI exe cross-compiled on macOS (xwin, then mingw) | release-cli | — | todo | | task file pending; exe checked on Windows by the user |
| T-02 `cargo` tool: detection, `Cargo.toml` + `Cargo.lock` versions | release-cli | go-apps T-01 | todo | | task file pending; parallel to T-01 |

### Phase 2: build connector
| Task | Repo | Depends on | Status | PR | Note |
|---|---|---|---|---|---|
| T-03 `cargo-build` connector and Rust fixture | release-cli | T-01, T-02, go-apps T-03, T-04 | todo | | task file pending; reuses go-build's layout and manifest code |

### Phase 3: first consumer
| Task | Repo | Depends on | Status | PR | Note |
|---|---|---|---|---|---|
| T-04 The Windows desktop app releases with release-cli (acceptance) | consumer app | P2, go-apps T-05, T-08 | todo | | task file pending |

## Open questions
- Windows Authenticode signing (SmartScreen warning on an unsigned exe): certificate, cost, where the key lives — user.
- Also attach the bare `.exe` next to the `.zip` (a desktop app is often downloaded as the exe)? It would add a
  `binary` kind to the go-apps manifest — user.
- Should the first consumer drop its hosted Windows build job once local releases work, or keep it for PR checks? —
  consumer's own plan.

## Log
- 2026-11-22: requirement written from the first consumer's setup (Cargo workspace, edition 2024, eframe/glow, one
  Windows binary). Rebased on go-apps: local build connector (user's choice), shared manifest, `sign` and
  `github-release` assets. Task files not written yet.
