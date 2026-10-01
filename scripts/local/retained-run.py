#!/usr/bin/env python3
"""Explicit Pipeline run/replace with an append-only, 90-day local archive."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time

RETENTION_SECONDS = 90 * 24 * 60 * 60
MAX_BYTES = 2 * 1024**3
RESERVE_BYTES = 64 * 1024**2


def require(condition, message):
    if not condition:
        raise ValueError(message)


def safe_path(path):
    path = Path(os.path.abspath(path))
    for parent in [*reversed(path.parents), path]:
        if parent.exists() or parent.is_symlink():
            require(not parent.is_symlink(), f"symlink is not an archive input: {parent}")
    return path


def digest(path):
    with open(safe_path(path), "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    with open(path, "x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


@contextlib.contextmanager
def locked(path):
    path = safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def scope(repository):
    return hashlib.sha256(str(repository).encode()).hexdigest()


def files_below(root):
    root = safe_path(root)
    files = []
    for directory, children, names in os.walk(root, followlinks=False):
        for name in children + names:
            path = safe_path(Path(directory) / name)
            mode = path.stat().st_mode
            require(stat.S_ISDIR(mode) or stat.S_ISREG(mode), f"non-regular archive input: {path}")
        files.extend(Path(directory) / name for name in names)
    return sorted(files)


def copy_file(source, destination):
    source = safe_path(source)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        require(stat.S_ISREG(os.fstat(fd).st_mode), "archive input is not regular")
        with os.fdopen(fd, "rb", closefd=False) as incoming, open(destination, "xb") as outgoing:
            os.chmod(destination, 0o600)
            shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
            outgoing.flush()
            os.fsync(outgoing.fileno())
    finally:
        os.close(fd)


def verify_archive(directory):
    directory = safe_path(directory)
    manifest = json.loads(safe_path(directory / "manifest.json").read_text())
    require(manifest["retainUntilEpoch"] - manifest["archivedAtEpoch"] >= RETENTION_SECONDS,
            "archive retention is shorter than 90 days")
    expected = set(manifest["files"])
    actual = {p.relative_to(directory).as_posix() for p in files_below(directory)}
    require(actual == expected | {"manifest.json"}, "archive member set changed")
    for name, identity in manifest["files"].items():
        relative = Path(name)
        require(not relative.is_absolute() and ".." not in relative.parts, "archive traversal")
        path = directory / relative
        require(path.stat().st_size == identity["bytes"] and digest(path) == identity["sha256"],
                f"archive content changed: {name}")
    return manifest


def archive_run(run_directory, status, archive, *, superseded=False, now=None):
    """Preserve terminal evidence; a historical/stale pass never becomes current."""
    run_directory, archive = safe_path(run_directory), safe_path(archive)
    run_id = status["runId"]
    require(re.fullmatch(r"[0-9]+-[0-9]+", run_id), "invalid run ID")
    require(run_directory.name == run_id, "run directory does not match result")
    require(status["outcome"] != "running", "cannot archive a running task")
    raw = json.loads(safe_path(run_directory / "record.json").read_text())
    require(raw.get("outcome") != "running", "native record has no terminal seal")
    for key, value in raw.items():
        if key not in ("freshness", "releaseEligible"):
            require(value == status.get(key), f"native result identity mismatch: {key}")
    target = archive / scope(status["repository"]) / run_id
    with locked(archive / ".archive.lock"):
        if target.exists():
            previous = verify_archive(target)
            require(previous["recordSha256"] == raw["recordSha256"], "run archive collision")
            return target
        selected = [run_directory / "record.json"]
        for name in ("preparation.json", "run.log"):
            if (run_directory / name).exists():
                selected.append(run_directory / name)
        if (run_directory / "reports").exists():
            selected.extend(files_below(run_directory / "reports"))
        if "package" in raw and raw["package"]:
            artifact = run_directory / "package/artifact"
            require(artifact.stat().st_size == raw["package"]["bytes"] and
                    digest(artifact) == raw["package"]["sha256"], "native package hash mismatch")
            selected.append(artifact)
        for name, expected in raw.get("workflowInputs", {}).items():
            relative = Path(name)
            require(not relative.is_absolute() and ".." not in relative.parts, "workflow traversal")
            original = run_directory / "workflow" / relative
            # The native runner binds a private Task path in environment.json.
            # Preserve that executed file and its actual digest, plus the native
            # record's original input hashes; do not copy the Task executable.
            require(original.is_file(), "retained workflow input missing")
            if name == "environment.json":
                expected = raw.get("resolvedWorkflowEnvironmentSha256", expected)
            require(digest(original) == expected, "retained workflow input changed")
            selected.append(original)
        size = sum(safe_path(path).stat().st_size for path in selected)
        require(size <= MAX_BYTES, "archive input exceeds 2 GiB bound")
        archive.mkdir(parents=True, exist_ok=True, mode=0o700)
        require(shutil.disk_usage(archive).free > size + RESERVE_BYTES,
                "insufficient archive storage; retain native evidence and free verified disposable space")
        stage = Path(tempfile.mkdtemp(prefix=".incoming-", dir=archive))
        try:
            for source in selected:
                copy_file(source, stage / source.relative_to(run_directory))
            write_json(stage / "observed-status.json", status)
            epoch = int(time.time() if now is None else now)
            manifest = {
                "schemaVersion": 1, "runId": run_id, "repository": status["repository"],
                "recordSha256": raw["recordSha256"], "archivedAtEpoch": epoch,
                "retainUntilEpoch": epoch + RETENTION_SECONDS,
                "superseded": superseded,
                "acceptedPackageAtArchiveTime": not superseded and status["outcome"] == "passed" and
                    status.get("freshness") == "current" and status.get("releaseEligible") is True and
                    bool(raw.get("package")),
                "files": {p.relative_to(stage).as_posix(): {"bytes": p.stat().st_size, "sha256": digest(p)}
                          for p in files_below(stage)},
            }
            write_json(stage / "manifest.json", manifest)
            verify_archive(stage)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.rename(stage, target)
            fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            if stage.exists():
                shutil.rmtree(stage)  # Only this invocation's unpublished staging directory.
    return target


class Pipeline:
    def __init__(self, executable, repository, workflow):
        self.executable = safe_path(executable).resolve(strict=True)
        self.repository = safe_path(repository).resolve(strict=True)
        self.workflow = safe_path(workflow).resolve(strict=True)
        self.runs = safe_path(self.call("inspect")["stateRoot"]) / "runner/runs"

    def call(self, command, *arguments):
        tail = ([command, arguments[0], str(self.repository), *arguments[1:]]
                if command == "onboarding" else [command, str(self.repository), *arguments])
        result = subprocess.run([str(self.executable), "--workflow", str(self.workflow), *tail],
                                capture_output=True, text=True, timeout=30)
        require(result.returncode == 0, f"Pipeline {command} failed: {result.stdout.strip()} {result.stderr.strip()}")
        value = json.loads(result.stdout)
        require(value.get("ok") is not False, f"Pipeline rejected {command}: {value.get('error')}")
        if "repository" in value:
            require(value["repository"] == str(self.repository), "foreign Pipeline owner/result")
        return value

    def control(self, operation, *arguments):
        return self.call("control", operation, *arguments)

    def ready(self):
        state = self.control("status")
        require(state.get("trusted") is True and state.get("paused") is True and not state.get("error"),
                "start the exact reviewed Pipeline owner paused; this command never grants trust")
        return state


def wait_until(predicate, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.1)
    raise TimeoutError("Pipeline did not reach the expected state; inspect status before retrying")


def execute(client, archive, entry, replace=False, review=None):
    archive = safe_path(archive)
    with locked(archive / f".{scope(client.repository)}.control.lock"):
        state = client.ready()
        if state["running"] or state["pending"]:
            require(replace, "local work is active/pending; use replace to supersede this workflow")
            client.control("cancel")
            state = wait_until(lambda: (s if not s["running"] and not s["pending"] else None)
                               if (s := client.control("status")) else None)
            if state.get("latestRunId"):
                previous = client.call("status", state["latestRunId"])
                archive_run(client.runs / state["latestRunId"], previous, archive, superseded=True)
        before = {p.name for p in client.runs.iterdir() if p.is_dir()}
        if entry == "package":
            require(review and re.fullmatch(r"[a-f0-9]{64}", review), "package requires its explicit review ID")
            client.control("package", review)
        else:
            client.control("run" if entry == "check:fast" else "full")

        def started():
            added = [p for p in client.runs.iterdir() if p.is_dir() and p.name not in before]
            require(len(added) <= 1, "another operator started a run; inspect before retrying")
            return added[0].name if added and (added[0] / "record.json").is_file() else None

        run_id = wait_until(started)

    # The transition lock is released while the task runs: a second explicit
    # replace can cancel it through the same native owner, without PID guessing.
    def terminal():
        result = client.call("status", run_id)
        return result if result["outcome"] != "running" else None

    result = wait_until(terminal, seconds=3700)
    with locked(archive / f".{scope(client.repository)}.control.lock"):
        allocated = [p.name for p in client.runs.iterdir() if p.is_dir() and
                     re.fullmatch(r"[0-9]+-[0-9]+", p.name)]
        superseded = max(allocated, key=lambda name: tuple(map(int, name.split("-")))) != run_id
        saved = archive_run(client.runs / run_id, result, archive, superseded=superseded)
    return {"runId": run_id, "archive": str(saved), "outcome": result["outcome"],
            "freshness": result["freshness"], "superseded": superseded, "acceptedPackageAtArchiveTime": verify_archive(saved)["acceptedPackageAtArchiveTime"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("run", "replace", "archive", "verify"))
    parser.add_argument("--pipeline", type=Path)
    parser.add_argument("--repository", type=Path)
    parser.add_argument("--workflow", type=Path)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--entry", choices=("check:fast", "check", "package"), default="package")
    parser.add_argument("--review")
    parser.add_argument("--run-id")
    args = parser.parse_args()
    try:
        if args.operation == "verify":
            result = verify_archive(args.archive)
        else:
            require(args.pipeline and args.repository and args.workflow, "select exact Pipeline, repository and workflow paths")
            client = Pipeline(args.pipeline, args.repository, args.workflow)
            if args.operation == "archive":
                require(args.run_id and re.fullmatch(r"[0-9]+-[0-9]+", args.run_id), "select a terminal run ID")
                status = client.call("status", args.run_id)
                result = {"archive": str(archive_run(client.runs / args.run_id, status, args.archive))}
            else:
                result = execute(client, args.archive, args.entry, args.operation == "replace", args.review)
        print(json.dumps(result, sort_keys=True))
        return 1 if (result.get("outcome", "passed") != "passed" or
                     result.get("freshness", "current") != "current" or result.get("superseded", False)) else 0
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
