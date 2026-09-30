# PX4XPlane `release.yml` workflow map

Source: published `master` at `b1969bf0050dd8e6e1a4393403e53db64b7b0240`.
This is an inventory and local handoff only; it does not qualify publication or
retire the Actions workflow.

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

## Remaining #29 acceptance

- Implemented locally: Git-shared explicit `release:prepare`, `release:stage-draft`
  and `release:publish` tasks. They require a selected version tag at the exact
  clean source commit, a passing macOS package report, and matching package
  bytes. Draft creation and public publication each require the exact tag as
  operator confirmation. Draft readback is checked before publication. The
  former manual-dispatch `dev-YYYYMMDD-HHMMSS` tag is supported as an explicit
  operator-selected prerelease tag.
- Focused fixture qualification: `node --test tests/local-release.test.mjs` —
  8 passed, 0 failed. It covers source/tag identity, unchanged package bytes,
  dirty-source rejection, exact-tag confirmation, simulated draft/publish
  readback, asset digest rejection and manual-dispatch prerelease tagging. This
  is fixture evidence, not live GitHub authentication or publication.
- Owner input still required: confirm an authorized GitHub credential route
  for this repository's `contents:write` release operation. The helper clears
  token environment variables before invoking GitHub CLI; the CLI uses its
  configured local credential store. No token is printed or placed in plan or
  report files. The host's previously checked GitHub CLI credential was
  rejected, so no live attempt is claimed.
- No production release has been staged or published. Do not invoke either
  network task until the credential route and a release target are approved.
- Keep `.github/workflows/release.yml` enabled until the local operation,
  explicit publication/readback contract, trigger/status disposition, and
  required review are accepted. Preserve the separate product release policy.

The existing package receipt in
`/Users/briankeeley/sprig/audit-reports/px4xplane-cutover-99e4e82-2026-09-30/README.md`
qualifies only its exact local macOS arm64 package candidate. It is not a
release-publication receipt.
