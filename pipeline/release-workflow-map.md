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

- Implemented candidate: a Git-shared generator creates one immutable external
  Pipeline workflow per selected tag, passing local package run and explicit
  `stage` or `publish` operation. `check:fast` plans without network access;
  reviewed `package` performs only that operation and retains a receipt;
  `check` reads the remote tag/release/asset back. The source checkout must be
  clean and tagged at the package run's exact commit. The retained Pipeline
  package record, report and archive must agree on the macOS asset digest.
  `dev-YYYYMMDD-HHMMSS` is an explicitly selected prerelease. Unlike the old
  manual workflow, it is marked as a prerelease rather than an ordinary release.
- Focused local qualification: `node --test tests/managed-release.test.mjs`
  exercised the generator, tamper rejection, simulated draft/readback/publish,
  and the selected native Pipeline runner's `check:fast`, `package`, `check`
  with an isolated test trust identity and fake GitHub CLI. This verifies the
  managed execution boundary and failure path, not actual GitHub publication.
- The authenticated host CLI read route works, but the original Task labels
  could not run in the selected native runner, and its isolated HOME could not
  read that login. The generated workflow uses the runner's supported finite
  entries and passes explicitly reviewed host/config directory paths only to
  the `gh` child. Token variables are removed; no token bytes enter Task input,
  source, reports or logs. Live release-write scope and credential availability
  in the selected managed process are still unverified.
- No production release has been staged or published. A final source-bound
  macOS package run, exact pushed tag, reviewed workflow trust and explicit
  owner authorization for the target draft/publication are required before a
  real write. No fixture result stands in for that readback.
- Keep `.github/workflows/release.yml` enabled until the local operation,
  explicit publication/readback contract, trigger/status disposition, and
  required review are accepted. Preserve the separate product release policy.

The existing package receipt in
`/Users/briankeeley/sprig/audit-reports/px4xplane-cutover-99e4e82-2026-09-30/README.md`
qualifies only its exact local macOS arm64 package candidate. It is not a
release-publication receipt.

## Draft readback and recovery

GitHub can return 404 from the release-by-tag endpoint for an unpublished draft.
The local publisher resolves the exact tag through the authenticated release
list and then reads the selected release ID. A retry of `stage` reuses only a
draft whose tag, source, notes, prerelease flag and sole asset match the reviewed
request. It never overwrites an existing release. A mismatched or published
release fails closed; inspect it before choosing any separate corrective action.

The October 2 managed attempt created draft release `401686395` for
`dev-20260930-225214` and uploaded the qualified 164,807-byte macOS asset, then
failed in its old tag-based readback. Authenticated listing/readback verifies the
actual draft and digest. [The retained evidence](../docs/evidence/pipeline-draft-readback-2026-10-02.json)
separates that remote write, the failed native result, focused repair tests and
remaining managed recovery. Do not retry the old publisher or treat the draft
as publicly published. `release.yml` remains enabled.
