# Local Pipeline build workflow

`pipeline/workflows/local-build/` is the Git-shared, reviewed local workflow
bundle. It binds the source inputs and four pinned Git submodules, uses
Pipeline-managed Task, and keeps build, report and package outputs in the
isolated Pipeline run. No GitHub status or hosted service is involved.

Prepare and inspect the exact checkout in Pipeline, then explicitly invoke:

```sh
pipeline onboarding inspect /absolute/path/to/sprig-px4xplane
pipeline onboarding approve /absolute/path/to/sprig-px4xplane REVIEW_ID --consent
pipeline onboarding setup /absolute/path/to/sprig-px4xplane --consent --gate manual
pipeline diagnose /absolute/path/to/sprig-px4xplane --workflow /absolute/path/to/sprig-px4xplane/pipeline/workflows/local-build
pipeline run /absolute/path/to/sprig-px4xplane check:fast --workflow /absolute/path/to/sprig-px4xplane/pipeline/workflows/local-build
pipeline run /absolute/path/to/sprig-px4xplane check --workflow /absolute/path/to/sprig-px4xplane/pipeline/workflows/local-build
pipeline package-preview /absolute/path/to/sprig-px4xplane --workflow /absolute/path/to/sprig-px4xplane/pipeline/workflows/local-build
pipeline run /absolute/path/to/sprig-px4xplane package --workflow /absolute/path/to/sprig-px4xplane/pipeline/workflows/local-build
```

Use the Pipeline desktop Runs view or its local `status`, `log` and `inspect`
commands to view results. `check` verifies the CMake Release output and its
copied config, README and airframe files. `package` requires a clean committed
source and explicit review of the exact package candidate; Pipeline retains the
bounded archive with its source/workflow identity. It does not publish or deploy
the plugin. Installed-app release and update policy is separate.

## Responsibility mapping for build.yml

| Existing Actions behavior | Git-shared local Pipeline replacement / disposition |
| --- | --- |
| `pull_request` targeting `master`; manual `workflow_dispatch`. Regular branch pushes do not run this workflow. Version tags are handled by the separately owned `release.yml`. | Local `check:fast`, `check`, or reviewed `package` is started explicitly by the developer using the commands above. There is no automatic GitHub event intake or forge status from a local run. The Actions triggers remain enabled until the remaining platform dispositions below are accepted. |
| `build` job, Windows 2022 / Ubuntu 22.04 / macOS 14 matrix, `fail-fast: false`. Each job checks out recursive submodules with full history. | The current Pipeline profile is native macOS 26.4.1 arm64 with four exact submodule pins and complete selected source. That is the only claimed local target. Windows, Linux, and universal output have no verified local profile and remain unqualified. |
| Windows installs MSBuild; Linux uses privileged `apt-get` for build packages; macOS checks the selected Xcode. All jobs configure CMake Release, build, then assert platform-specific binary paths and copy config/README/airframes. | The `check` and `package` tasks configure CMake Release, build the plugin, and run `pipeline/qualify.cmake` against the current host. The qualification report asserts the binary, config, README, and five airframe files. Setup uses declared, pinned Task/CMake/Ninja/Clang/Node inputs and does not run a privileged package manager. |
| No explicit `permissions:` block, environment, repository secret, OIDC route, or cache is declared. `actions/checkout` uses its implicit read token; artifact upload uses the Actions runtime token. | No credential is needed for local build/check/package. The package remains local and is not a release or deployment. No remote cache is used. |
| Matrix artifacts are uploaded with `if-no-files-found: error` and 90-day retention. The `build-summary` job has `needs: build`, `if: always()`, and exits nonzero unless all matrix jobs succeeded. | Pipeline retains the source-bound local package in its bounded run record (profile limit: 1 GiB per artifact; 2 GiB worktree state). This is not 90-day remote artifact retention. `check:fast` configures only; `check` builds and reports; `package` builds, verifies and retains the archive. Pipeline records failed checks and marks old results stale when inputs change. |
| Concurrency group is `${{ github.workflow }}-${{ github.ref }}` with `cancel-in-progress: true`; a newer PR/manual run cancels the superseded run. | Local runs are explicit and use a host concurrency limit of one. Pipeline does not infer a newer GitHub ref or cancel work automatically; the operator may cancel a local run and inspect its recorded outcome. |

The original job names are `Build ${{ matrix.platform }} (${{ matrix.os }})`
and `Build Summary`. They are GitHub statuses, not the names of local Pipeline
tasks. The old `Build …` statuses end when `build.yml` is removed. This issue owns only
`.github/workflows/build.yml`; `release.yml` and other product workflows remain
under their own dispositions.

Before invocation, check `pipeline capabilities` and confirm the selected
Pipeline CLI advertises `bounded-package-artifacts`,
`git-submodule-snapshots`, and `source-symlink-snapshots`. Publishing Pipeline
source does not upgrade an installed Pipeline.app or an already-running service.
Use a qualified CLI/runtime with those features; inspect its binary and service
identity if choosing another installation.

The committed profile qualifies macOS 26.4.1 arm64 only. Windows/Linux outputs,
universal packaging, superseded-run cancellation and 90-day remote artifact
retention remain deferred and unqualified; this cutover does not remove product
source or support code for those platforms. After the macOS local replacement
passed positive package and deliberate-failure checks, `.github/workflows/build.yml`
was retired. Its automatic pull-request/manual triggers and `Build …` GitHub
statuses are retired with it. Pipeline runs remain explicit and create no forge
statuses. Do not describe the former macOS binary as universal.

Install CMake, the X-Plane SDK inputs already tracked by this repository, the
pinned Task tool and platform compiler/linker dependencies before setup. The
workflow does not run privileged system package managers. Windows/Linux require
their own supported local profiles and qualification before being claimed.
