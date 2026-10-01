# Local Pipeline build workflow

`pipeline/workflows/local-build/` is the Git-shared, reviewed local workflow
bundle. It binds the source inputs and four pinned Git submodules, uses
Pipeline-managed Task, and keeps build, report and package outputs in the
isolated Pipeline run. No GitHub status or hosted service is involved.

Use Python 3.11 or newer for the stdlib-only archive helper.
Select an explicitly qualified native CLI and pin its executable/hash in the
qualification receipt. A source merge does not upgrade the installed app.
The workflow and source below must be reviewed in Pipeline before execution:

```sh
PIPELINE=/absolute/path/to/qualified/native/pipeline
SOURCE=/absolute/path/to/sprig-px4xplane
WORKFLOW="$SOURCE/pipeline/workflows/local-build"
export PIPELINE_HOME=/absolute/path/to/this-qualification-state
ARCHIVE=/Users/briankeeley/sprig/pipeline-artifact-archive/px4xplane

"$PIPELINE" --workflow "$WORKFLOW" onboarding inspect "$SOURCE"
# Review the exact displayed files/permissions; approve only that REVIEW_ID.
"$PIPELINE" --workflow "$WORKFLOW" onboarding approve "$SOURCE" REVIEW_ID --consent
"$PIPELINE" --workflow "$WORKFLOW" diagnose "$SOURCE"
# In a separate terminal, keep this explicit local owner paused:
"$PIPELINE" --workflow "$WORKFLOW" serve "$SOURCE" --trust --paused
```

The paused owner starts no automatic source-change builds. The repository-owned
helper invokes the existing native control boundary. It never grants trust,
starts a service, selects a new source revision, publishes an artifact or kills
a process by a guessed PID. In another terminal using the same `PIPELINE_HOME`:

```sh
python3 "$SOURCE/scripts/local/retained-run.py" replace \
  --pipeline "$PIPELINE" --repository "$SOURCE" --workflow "$WORKFLOW" \
  --archive "$ARCHIVE" --entry check
"$PIPELINE" --workflow "$WORKFLOW" package-preview "$SOURCE"
# Review the exact package candidate and use its PACKAGE_REVIEW_ID:
python3 "$SOURCE/scripts/local/retained-run.py" replace \
  --pipeline "$PIPELINE" --repository "$SOURCE" --workflow "$WORKFLOW" \
  --archive "$ARCHIVE" --entry package --review PACKAGE_REVIEW_ID
```

`run` refuses active/pending work. `replace` cancels only the selected
repository/workflow through Pipeline, waits for its terminal record, archives
that evidence, then requests the new run. The transition is serialized across
competing helpers; the lock is released during execution so another explicit
replacement can cancel it. A different workflow is rejected by the native
owner. Superseded/stale results are diagnostic, never accepted as a new pass.
Cancellation and failure preserve their records and available reports.

Use Pipeline's Runs view or `status SOURCE RUN_ID` and `log SOURCE RUN_ID` with
the same explicit workflow to inspect the native result. The helper prints the
run ID and retained archive path. A timeout or disconnected client is ambiguous:
inspect native status before retrying; do not replay a mutating command blindly.
Stop the owner when finished with `control SOURCE shutdown` using the same
workflow. Do not issue competing direct `control run/full/package` operations
while using the helper; use the helper's serialized run/replace commands.

### Local archive policy and recovery

Each terminal record, source/workflow identities, available reports/log, and
retained package is copied to `ARCHIVE/<repository-path-hash>/<run-id>`.
Credential homes, build directories and Task binaries are not copied. Writes
are private, staged, hashed, read back and atomically published. Each manifest
records `archivedAtEpoch` and a `retainUntilEpoch` at least 90 days later.
Normal commands **never delete old archives**, even after that deadline, and
refuse insufficient storage. Keep this archive on retained operator storage;
do not manually remove its sole copy before `retainUntilEpoch`. A local disk
loss is not hosted redundancy; explicitly export verified copies when needed.

`acceptedPackageAtArchiveTime` describes the native status when copied. It is
not an enduring claim of freshness, release authorization or qualification for
another source. Historical artifacts retain their original source and hashes.

```sh
python3 "$SOURCE/scripts/local/retained-run.py" verify --archive "$EXACT_RUN_ARCHIVE"
# Explicit export to a new owner-selected directory, then verify the copy:
cp -R "$EXACT_RUN_ARCHIVE" "$NEW_EXPORT_DIRECTORY"
python3 "$SOURCE/scripts/local/retained-run.py" verify --archive "$NEW_EXPORT_DIRECTORY"
```

If archiving fails or the helper exits, preserve native state, repair storage,
then recover the exact terminal run without rebuilding it:

```sh
python3 "$SOURCE/scripts/local/retained-run.py" archive \
  --pipeline "$PIPELINE" --repository "$SOURCE" --workflow "$WORKFLOW" \
  --archive "$ARCHIVE" --run-id RUN_ID
```

An existing archive is verified and reused without resetting its retention
clock. Changed bytes, symlinks, foreign results, package hash mismatch and
incomplete records fail closed. A new source needs its own applicable evidence;
archive import does not qualify a new build.

## Responsibility mapping for build.yml

| Existing Actions behavior | Git-shared local Pipeline replacement / disposition |
| --- | --- |
| `pull_request` targeting `master`; manual `workflow_dispatch`. Regular branch pushes do not run this workflow. Version tags are handled by the separately owned `release.yml`. | Local `check:fast`, `check`, or reviewed `package` is started explicitly by the developer using the commands above. There is no automatic GitHub event intake or forge status from a local run. The owner approved retiring automatic PR/dispatch operation in favor of these explicit local commands; cutover waits for the final replacement receipt. |
| `build` job, Windows 2022 / Ubuntu 22.04 / macOS 14 matrix, `fail-fast: false`. Each job checks out recursive submodules with full history. | The current Pipeline profile is native macOS 26.4.1 arm64 with four exact submodule pins and complete selected source. That is the only claimed local target. Windows, Linux, and universal output have no verified local profile and remain unqualified. |
| Windows installs MSBuild; Linux uses privileged `apt-get` for build packages; macOS checks the selected Xcode. All jobs configure CMake Release, build, then assert platform-specific binary paths and copy config/README/airframes. | The `check` and `package` tasks configure CMake Release, build the plugin, and run `pipeline/qualify.cmake` against the current host. The qualification report asserts the binary, config, README, and five airframe files. Setup uses declared, pinned Task/CMake/Ninja/Clang/Node inputs and does not run a privileged package manager. |
| No explicit `permissions:` block, environment, repository secret, OIDC route, or cache is declared. `actions/checkout` uses its implicit read token; artifact upload uses the Actions runtime token. | No credential is needed for local build/check/package. The package remains local and is not a release or deployment. No remote cache is used. |
| Matrix artifacts are uploaded with `if-no-files-found: error` and 90-day retention. The `build-summary` job has `needs: build`, `if: always()`, and exits nonzero unless all matrix jobs succeeded. | Pipeline retains the source-bound local package in its bounded run record (profile limit: 1 GiB per artifact; 2 GiB worktree state). The helper preserves terminal records and packages for at least 90 days on local operator storage. The owner approved local access and explicit export in place of hosted/shared artifact access. `check:fast` configures only; `check` builds and reports; `package` builds, verifies and retains the archive. Pipeline records failed checks and marks old results stale when inputs change. |
| Concurrency group is `${{ github.workflow }}-${{ github.ref }}` with `cancel-in-progress: true`; a newer PR/manual run cancels the superseded run. | Local runs are explicit and use a host concurrency limit of one. The explicit `replace` helper uses native cancellation and archives the superseded terminal result before starting selected local work. The owner approved retirement of automatic GitHub supersession. |

The original job names are `Build ${{ matrix.platform }} (${{ matrix.os }})`
and `Build Summary`. They are GitHub statuses, not the names of local Pipeline
tasks. The owner approved retiring automatic PR/dispatch triggers, automatic
GitHub supersession and hosted/shared artifacts after the local replacement
qualifies. Local runs create no forge status or merge enforcement. This issue
owns only `.github/workflows/build.yml`; `release.yml` and product publication
remain under their own dispositions.

Before invocation, check `pipeline capabilities` and confirm the selected
Pipeline CLI advertises `bounded-package-artifacts`,
`git-submodule-snapshots`, and `source-symlink-snapshots`. Publishing Pipeline
source does not upgrade an installed Pipeline.app or an already-running service.
Use a qualified CLI/runtime with those features; inspect its binary and service
identity if choosing another installation.

The committed local profile qualifies macOS 26.4.1 arm64 only. The owner
approved deferring Windows/Linux and universal distribution artifacts while
preserving their source support. Do not describe the local binary as universal.
The final local cancellation/archive receipt must pass before `build.yml` is
removed from the published branch. The older package receipts retain their
exact source scope; native fixture tests do not qualify the product package.

Install CMake, the X-Plane SDK inputs already tracked by this repository, the
pinned Task tool and platform compiler/linker dependencies before setup. The
workflow does not run privileged system package managers. Windows/Linux require
their own supported local profiles and qualification before being claimed.

## Explicit local release workflow

The separate `release.yml` replacement uses a finite, generated Pipeline
workflow for one selected tag, package receipt and remote operation. First run
the normal reviewed `package` entry and retain its passing run directory. Use a
pushed version tag at that exact clean source commit. A reviewed manual
`dev-YYYYMMDD-HHMMSS` tag remains a prerelease. The generator checks the tag,
source, package report, retained artifact and SHA-256, then writes a new
workflow directory outside the repository. It contains no credential bytes.
Select the qualified Task binary and, for authenticated operations, the existing
GitHub CLI Keychain home/config directories by path:

```sh
node scripts/local/managed-release-workflow.mjs generate \
  "$SOURCE" "$PASSING_PACKAGE_RUN" "$TAG" "$NEW_STAGE_WORKFLOW" stage "$TASK_BINARY" \
  --auth-home "$GH_HOME" --gh-config-dir "$GH_CONFIG_DIR"
pipeline run "$SOURCE" check:fast --workflow "$NEW_STAGE_WORKFLOW"
pipeline --workflow "$NEW_STAGE_WORKFLOW" onboarding inspect "$SOURCE"
# A human reviews the exact workflow and approves its displayed REVIEW_ID:
pipeline --workflow "$NEW_STAGE_WORKFLOW" onboarding approve "$SOURCE" "$REVIEW_ID" --consent
pipeline package-preview "$SOURCE" --workflow "$NEW_STAGE_WORKFLOW"
pipeline run "$SOURCE" package --workflow "$NEW_STAGE_WORKFLOW"
pipeline run "$SOURCE" check --workflow "$NEW_STAGE_WORKFLOW"
```

`check:fast` is a no-network local plan; `package` is the reviewed, explicit
remote mutation; `check` reads the remote release and tag back. The stage
operation creates a draft with only the qualified macOS arm64 archive. For
publication, generate a **new** workflow using `publish` and the same tag and
passing package run, review that request, then repeat the `check:fast`, trust
review, `package-preview`, `package` and `check` sequence with that new workflow.

```sh
node scripts/local/managed-release-workflow.mjs generate \
  "$SOURCE" "$PASSING_PACKAGE_RUN" "$TAG" "$NEW_PUBLISH_WORKFLOW" publish "$TASK_BINARY" \
  --auth-home "$GH_HOME" --gh-config-dir "$GH_CONFIG_DIR"
```

The publish `package` action verifies the staged draft's source and asset digest
before making it public. Pipeline retains a receipt archive for
each mutation. View results through Pipeline's Runs view or local `status`, `log`
and `inspect` commands. A failed run after a remote write requires readback and
reconciliation before retrying; never overwrite an asset to force a pass.

The selected credential directories are passed only to the GitHub CLI child.
Ambient token variables are removed. No token is placed in the workflow,
Task environment, logs or source. The ordinary build/check/package workflow
has no remote publication step. Pipeline's local workflow trust review is
required before either remote `package` action. Authentication in Pipeline's
isolated process and release-write scope remain to be qualified; host CLI read
access does not establish them.

No release was staged or published by this candidate. Keep
`.github/workflows/release.yml` enabled until required review, managed
execution, authorized draft/publication and readback pass. Windows/Linux and
universal release assets remain deferred by the owner decision recorded for #28.
