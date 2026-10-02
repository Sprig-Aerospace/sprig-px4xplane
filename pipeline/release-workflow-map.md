# PX4XPlane `release.yml` workflow map

Source: published `master` at `b1969bf0050dd8e6e1a4393403e53db64b7b0240`.
The original contract below is retained for comparison. The local replacement
and explicit retirement are accepted by the evidence at the end of this page.

| Existing behavior | Exact contract / local disposition |
| --- | --- |
| Trigger | Push tags matching `v*.*.*`, or manual `workflow_dispatch`. Manual runs use a generated `dev-YYYYMMDD-HHMMSS` version. There is no concurrency group. |
| Release creation | An Ubuntu job checks out full history, generates changelog text from Git tags, then creates a non-draft GitHub Release before any platform build succeeds. `contents: write` is declared; `softprops/action-gh-release@v1` is not pinned to a commit. Manual runs are prereleases only when the generated name contains `alpha`, `beta`, or `rc`. |
| Build matrix | Windows 2022, Ubuntu 22.04, and macOS 14 run in parallel with `fail-fast: false`. Windows installs MSBuild; Linux uses privileged `apt-get`; macOS attempts `brew install zip`. Each configures CMake Release and builds the platform plugin. |
| Assets | `px4xplane-windows-<version>.zip`, `px4xplane-linux-<version>.zip`, and `px4xplane-macos-<version>.zip` are uploaded to the already-created release. The release changelog labels macOS output Universal, while the workflow matrix itself selects one macOS runner and platform configuration; do not infer universal qualification from that label. |
| Failure and retention | The release exists before build completion, so failed/partial matrix jobs can leave an incomplete public release. The final summary job runs with `always()` and fails unless every matrix job succeeds. No explicit artifact retention, cache, environment, or recovery transaction is configured. |
| Credential route | GitHub Actions' implicit token is granted `contents: write` for release creation and uploads. Local tasks must use an explicit, operator-authorized local publication operation; no credential is passed into ordinary Pipeline checks or package Tasks. |
| Local replacement today | `pipeline/workflows/local-build` provides explicit `check:fast`, `check`, and `package` tasks. Results and package bytes remain in the local Pipeline run for review. It does not create a GitHub Release or upload assets. Its current qualified profile is macOS 26.4.1 arm64 only. |
| Current owner disposition | On 2026-09-30 the owner accepted deferring Windows/Linux/universal outputs as future distribution work, preserving their source/build support. That decision is recorded for #28. It does not qualify or authorize publishing a macOS release. |

## Accepted local replacement — 2026-10-02

The Git-shared generator produces an immutable external workflow per selected
source/tag/package and `stage` or `publish` operation. `check:fast` plans locally;
reviewed `package` performs that exact network operation; `check` reads it back.
The package must already have a passing clean-source native record, report and
matching retained bytes. Full Git history supplies release notes. Local workflow
trust and explicit release authorization remain required for every selected
operation. The existing GitHub CLI Keychain route is passed only to the `gh`
child; token bytes never enter source, workflow files, or reports.

[Exact qualification receipt](../docs/evidence/pipeline-release-acceptance-2026-10-02.json):

- Original product source `9dbb408e7e062134c1a7ac2c4c43225e1efa903d`; passing
  package run `1790808674908-28172` was reused without rebuilding.
- Published helper `c70733d` recovered existing draft 401686395 through real
  managed package/check runs `1790968848728-40253` / `1790968876821-41611`.
- Owner-authorized managed publication/readback runs
  `1790968926026-42745` / `1790968959991-44180` passed for development prerelease
  `dev-20260930-225214`. An unauthenticated public download matched the original
  164,807-byte asset and SHA-256 `8533d4c5ee3f57335c92dc3ea5ed984c50a75d15da14c1db86c03084303fa557`.
- Deliberate managed read-only check `1790969027901-45258` rejected the old draft
  expectation after publication. The earlier failed post-create readback remains
  retained; its partial write was reconciled without a duplicate or overwrite.
- Native runner source `5d97317`, binary hash `97253c8f7ed4387d4d2fac13d419b00b72446c8ef9a4776350d75812637ed0d2`,
  Task 3.53.1 and Node 24.14.1 were selected explicitly. Source/helper/workflow,
  package and archive identities are bound in the receipt.

## Trigger, status and platform retirement

The owner-approved local contract retires automatic `v*.*.*` tag dispatch,
GitHub manual Actions dispatch and the `Create GitHub Release`, three
`Build <platform> for Release`, and `Release Summary` forge checks.
Pushing a tag alone does not build or publish. Users invoke the reviewed local
workflow and inspect Pipeline Runs, `status`/`log` and retained receipts as
documented in [the operator runbook](README.md#explicit-local-release-workflow).
No hosted check service or automatic event receiver replaces these triggers.

Only macOS 26.4.1 arm64 is qualified. Windows/Linux/universal distribution remains
owner-deferred; source/build support is preserved. The actual artifact is the
verified macOS arm64 tar.gz, not the former three ZIP/universal promise.
No stable release or X-Plane/HITL runtime acceptance is implied.

`release.yml` had no concurrency group, automatic cancellation, cache,
environment gate or artifact-retention period. Explicit local cancellation
preserves terminal evidence; a cancellation or error after a remote write may
leave a completed write, so inspect and reconcile the exact release before any
retry. Draft recovery validates tag/source/notes/sole asset and refuses changed
or already-published state. No rollback deletes or overwrites remote assets.
Local receipts/packages are archived for at least 90 days; public assets retain
normal GitHub release lifetime. Authenticated staging, public availability,
negative-state rejection and recovery are now evidenced, so only
`.github/workflows/release.yml` is removed for #29.
