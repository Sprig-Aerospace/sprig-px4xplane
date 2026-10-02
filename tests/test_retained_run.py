"""Archive unit checks and optional real native-supervisor integration.

PIPELINE_TEST_RUNNER and PIPELINE_TEST_TASK select exact qualified binaries.
Native integration uses only temporary source/workflow/state/trust identities.
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/local/retained-run.py"
SPEC = importlib.util.spec_from_file_location("retained_run", SCRIPT)
retained = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(retained)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="px4-retention-test-")
        self.root = Path(self.temp.name).resolve()
        self.run = self.root / "100-1"
        self.run.mkdir()
        self.archive = self.root / "archive"
        (self.run / "package").mkdir()
        artifact = self.run / "package/artifact"
        artifact.write_bytes(b"fixture package")
        self.status = {"runId": "100-1", "repository": "/fixture/source", "outcome": "passed",
                       "freshness": "current", "releaseEligible": True, "recordSha256": "fixture-seal",
                       "package": {"bytes": artifact.stat().st_size, "sha256": retained.digest(artifact)}}
        (self.run / "record.json").write_text(json.dumps(self.status))

    def tearDown(self):
        self.temp.cleanup()

    def save(self, **kwargs):
        return retained.archive_run(self.run, self.status, self.archive, **kwargs)

    def test_retains_exact_bytes_and_at_least_ninety_days_without_purge(self):
        directory = self.save(now=1000)
        manifest = retained.verify_archive(directory)
        self.assertEqual(manifest["retainUntilEpoch"], 1000 + 90 * 86400)
        self.assertTrue(manifest["acceptedPackageAtArchiveTime"])
        self.assertEqual((directory / "package/artifact").read_bytes(), b"fixture package")
        for moment in (1000 + 90 * 86400 - 1, 1000 + 90 * 86400, 1000 + 100 * 86400):
            self.assertEqual(self.save(now=moment), directory)
            self.assertEqual(retained.verify_archive(directory), manifest)

    def test_stale_and_superseded_passes_are_diagnostic_only(self):
        self.status["freshness"] = "stale"
        directory = self.save()
        self.assertFalse(retained.verify_archive(directory)["acceptedPackageAtArchiveTime"])
        other = self.root / "other"
        self.status["freshness"] = "current"
        directory = retained.archive_run(self.run, self.status, other, superseded=True)
        self.assertFalse(retained.verify_archive(directory)["acceptedPackageAtArchiveTime"])
        self.assertTrue(retained.verify_archive(directory)["superseded"])

    def test_archive_reports_cancellation_without_inventing_a_package(self):
        self.status.update(outcome="cancelled", releaseEligible=False)
        self.status.pop("package")
        (self.run / "record.json").write_text(json.dumps(self.status))
        (self.run / "reports/check").mkdir(parents=True)
        (self.run / "reports/check/failure.txt").write_text("cancelled fixture")
        directory = self.save(superseded=True)
        self.assertIn("reports/check/failure.txt", retained.verify_archive(directory)["files"])
        self.assertFalse((directory / "package/artifact").exists())

    def test_insufficient_storage_preserves_native_evidence(self):
        with patch.object(retained.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(1, 1, 0)):
            with self.assertRaisesRegex(ValueError, "insufficient archive storage"):
                self.save()
        self.assertTrue((self.run / "package/artifact").exists())
        self.assertEqual(list(self.archive.glob(".incoming-*")), [])

    def test_tamper_symlink_and_foreign_run_are_rejected(self):
        directory = self.save()
        (directory / "package/artifact").write_bytes(b"altered")
        with self.assertRaisesRegex(ValueError, "archive content changed"):
            retained.verify_archive(directory)
        (self.run / "reports").symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            retained.archive_run(self.run, self.status, self.root / "second")
        self.status["repository"] = "/foreign/source"
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            retained.archive_run(self.run, self.status, self.root / "third")

    def test_competing_archivers_publish_one_complete_record(self):
        results, errors = [], []
        def save():
            try:
                results.append(self.save())
            except Exception as error:
                errors.append(error)
        workers = [threading.Thread(target=save) for _ in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(10)
            self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(set(results)), 1)
        retained.verify_archive(results[0])


@unittest.skipUnless(os.environ.get("PIPELINE_TEST_RUNNER") and os.environ.get("PIPELINE_TEST_TASK"),
                     "select exact native runner and Task for fixture integration")
class NativeSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="px4-native-cancel-")
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "source"
        self.workflow = self.root / "workflow"
        self.repo.mkdir()
        self.workflow.mkdir()
        self.archive = self.root / "archive"
        self.runner = Path(os.environ["PIPELINE_TEST_RUNNER"]).resolve()
        task = Path(os.environ["PIPELINE_TEST_TASK"]).resolve()
        self.environment = patch.dict(os.environ, {"PIPELINE_HOME": str(self.root / "state"),
                                                   "PIPELINE_DISCOVERY_STATE": str(self.root / "discovery")})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        (self.repo / "worker.py").write_text('''import json, pathlib, sys, time
entry = sys.argv[1]
time.sleep(2)
folder = pathlib.Path('.pipeline-state/reports') / ('check-fast' if entry == 'check:fast' else entry)
folder.mkdir(parents=True, exist_ok=True)
report = {'outcome':'passed','entry':entry,'profile':'retention-fixture','counts':{'fixture':1}}
(folder/'run.json').write_text(json.dumps(report))
(folder/'suite.json').write_text(json.dumps({'fixture':True}))
artifact = pathlib.Path('.pipeline-state/packages/artifact')
artifact.parent.mkdir(parents=True, exist_ok=True)
artifact.write_text('fixture package only')
''')
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Fixture", "-c",
                        "user.email=fixture@example.invalid", "commit", "-qm", "fixture source"], check=True)
        descriptor = {"schemaVersion": 2, "contractVersion": "pipeline.local-workflow/v2",
                      "pipelineCompatibility": {"runnerProtocol": 1, "requiredFeatures": ["bounded-package-artifacts", "committed-package-snapshots"]},
                      "profile": {"id": "retention-fixture", "supportedHosts": [{"os": "darwin", "architecture": "arm64"}]},
                      "tools": {"task": {"version": "3.53.1", "executable": str(task), "executableSha256": retained.digest(task)}},
                      "paths": {"sourceInputs": ["worker.py"], "ignoredOutputs": [".pipeline-state/**"]},
                      "environmentInputs": {"required": ["PATH"], "optional": [], "credentialsRequiredForOrdinaryChecks": False},
                      "resourceBudget": {"hostConcurrentRuns": 1, "stateStorageMiBPerWorktree": 256, "logMiBPerRun": 1},
                      "entryPoints": {}}
        task_lines = ["version: '3'", "tasks:"]
        for entry in ("check:fast", "check", "package"):
            output = "check-fast" if entry == "check:fast" else entry
            descriptor["entryPoints"][entry] = {"timeoutSeconds": 30, "requiredSuites": [{"id": "fixture", "minimumAssertions": 1,
                "report": f".pipeline-state/reports/{output}/suite.json"}]}
            task_lines += [f"  {entry}:", "    cmds:", f'      - cd "$PIPELINE_SOURCE_DIR" && /usr/bin/python3 worker.py {entry}']
        descriptor["entryPoints"]["package"].update(sourceMode="complete-commit", maximumArtifactMiB=1,
                                                        artifact=".pipeline-state/packages/artifact")
        (self.workflow / "environment.json").write_text(json.dumps(descriptor))
        (self.workflow / "Taskfile.yml").write_text("\n".join(task_lines) + "\n")
        self.client = retained.Pipeline(self.runner, self.repo, self.workflow)
        review = self.client.call("onboarding", "inspect")
        self.client.call("onboarding", "approve", review["reviewId"], "--consent")
        self.log = open(self.root / "owner.log", "w")
        self.owner = subprocess.Popen([str(self.runner), "--workflow", str(self.workflow), "serve", str(self.repo),
                                       "--trust", "--paused", "--attached"], stdin=subprocess.PIPE, stdout=self.log, stderr=self.log)
        self.addCleanup(self.stop_owner)
        def ready():
            if self.owner.poll() is not None:
                raise RuntimeError((self.root / "owner.log").read_text())
            try:
                return self.client.ready()
            except (ValueError, OSError):
                return None
        retained.wait_until(ready)
        # Paused startup can carry a queued automatic fast check; clear it.
        self.client.control("cancel")

    def stop_owner(self):
        if self.owner.poll() is None:
            try:
                self.client.control("shutdown")
            finally:
                self.owner.wait(timeout=15)
        self.owner.stdin.close()
        self.log.close()
        self.temp.cleanup()

    def test_native_replacement_cancels_owned_run_and_archives_both(self):
        results, errors = [], []
        def first():
            try:
                results.append(retained.execute(self.client, self.archive, "check"))
            except Exception as error:
                errors.append(error)
        worker = threading.Thread(target=first)
        worker.start()
        retained.wait_until(lambda: self.client.control("status")["running"])
        other_workflow = self.root / "different-workflow"
        shutil.copytree(self.workflow, other_workflow)
        foreign = retained.Pipeline(self.runner, self.repo, other_workflow)
        with self.assertRaisesRegex(ValueError, "different workflow"):
            retained.execute(foreign, self.archive, "check", replace=True)
        self.assertTrue(self.client.control("status")["running"])
        replacement = retained.execute(self.client, self.archive, "check", replace=True)
        worker.join(15)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results[0]["outcome"], "cancelled")
        self.assertEqual(replacement["outcome"], "passed")
        self.assertNotEqual(results[0]["runId"], replacement["runId"])
        self.assertTrue(retained.verify_archive(results[0]["archive"])["superseded"])
        retained.verify_archive(replacement["archive"])

    def test_native_package_archived_and_source_change_stays_stale(self):
        review = self.client.call("package-preview")
        result = retained.execute(self.client, self.archive, "package", review=review["reviewId"])
        self.assertTrue(result["acceptedPackageAtArchiveTime"],
                        (Path(result["archive"]) / "observed-status.json").read_text())
        results = []
        worker = threading.Thread(target=lambda: results.append(retained.execute(self.client, self.archive, "check")))
        worker.start()
        retained.wait_until(lambda: self.client.control("status")["running"])
        time.sleep(0.4)
        with open(self.repo / "worker.py", "a") as source:
            source.write("\n# changed while immutable snapshot runs\n")
        worker.join(15)
        self.assertFalse(worker.is_alive())
        self.assertNotEqual(results[0]["freshness"], "current")
        self.assertFalse(results[0]["acceptedPackageAtArchiveTime"])


if __name__ == "__main__":
    unittest.main()
