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

The committed profile currently qualifies macOS 26.4.1 arm64 only. The former
Actions workflow also built Windows and Linux, cancelled superseded PR/manual
runs and retained platform artifacts for 90 days. Those platform outputs and
retention are not qualified by the macOS result. Keep `.github/workflows/build.yml`
enabled until each applicable local replacement has positive and deliberate
failure evidence or its behavior has an explicit owner disposition. At cutover,
record that automatic pull-request/manual triggers and GitHub check enforcement
are retired; local runs are explicit and do not create forge statuses. Do not
describe the former macOS binary as universal.

Install CMake, the X-Plane SDK inputs already tracked by this repository, the
pinned Task tool and platform compiler/linker dependencies before setup. The
workflow does not run privileged system package managers. Windows/Linux require
their own supported local profiles and qualification before being claimed.
