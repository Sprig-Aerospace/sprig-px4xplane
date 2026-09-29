# Local Pipeline build workflow

`.github/workflows/build.yml` previously built Windows, Linux and macOS on pull
requests to `master` and manual dispatch, cancelled superseded runs, and retained
each platform's `px4xplane/` output for 90 days. Pipeline now has explicit local
`check:fast`, `check` and `package` tasks. The `check` task reproduces the CMake
Release build and verifies the platform binary plus copied config, README and
airframe files. `package` retains that plugin directory as a source-bound local
Pipeline artifact. It does not publish or deploy a plugin.

Prepare and review the exact checkout in Pipeline, then explicitly invoke:

```sh
pipeline onboarding inspect /absolute/path/to/sprig-px4xplane
pipeline onboarding approve /absolute/path/to/sprig-px4xplane REVIEW_ID --consent
pipeline onboarding setup /absolute/path/to/sprig-px4xplane --consent --gate manual
pipeline run /absolute/path/to/sprig-px4xplane check:fast
pipeline run /absolute/path/to/sprig-px4xplane check
pipeline run /absolute/path/to/sprig-px4xplane package
```

Use the Pipeline desktop Runs view or local `status`, `log` and `inspect`
commands to view results. The run receipt records the exact repository commit,
selected source-file hashes, profile, tool identities, task outputs and, for
`package`, the retained archive path, byte count and SHA-256. Keep task results
local; they do not create GitHub statuses. The old pull-request/manual triggers
and any implicit GitHub check enforcement are retired by this contract.

The committed profile qualifies macOS arm64 on macOS 26.4.1 only. The task graph
retains the Windows and Linux CMake output paths for future supported local
profiles, but does not claim those hosts have been qualified. Install CMake,
X-Plane SDK inputs already tracked by this repository, the pinned Task tool and
the platform's existing compiler/linker dependencies before setup. Linux package
installation and Windows MSVC provisioning are explicit owner operations; the
workflow does not run privileged system package managers. Do not remove the
Actions workflow until positive and deliberate-failure local receipts cover its
claimed platforms and artifact behavior.
