#!/usr/bin/env python3
"""Run repository validation tiers for Sprig px4xplane."""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ValidationCommand:
    name: str
    argv: tuple[str, ...]

    def shell_line(self) -> str:
        return " ".join(self.argv)


def build_commands(tier: str) -> list[ValidationCommand]:
    commands = [
        ValidationCommand(
            "px4-tcp-lifecycle-harness",
            ("python3", "tools/px4_tcp_lifecycle_harness.py", "--scenario", "all"),
        ),
        ValidationCommand(
            "package-script-syntax",
            ("bash", "-n", "scripts/package_macos.sh"),
        ),
    ]
    if tier in {"standard", "full"}:
        commands.extend(
            [
                ValidationCommand(
                    "cmake-configure",
                    ("cmake", "-S", ".", "-B", "build/macos-cmake", "-DCMAKE_BUILD_TYPE=Release"),
                ),
                ValidationCommand(
                    "macos-make-build",
                    ("make", "-f", "Makefile.macos", "BUILD_TYPE=Release"),
                ),
            ]
        )
    if tier == "full":
        commands.append(
            ValidationCommand(
                "package-macos",
                ("./scripts/package_macos.sh",),
            )
        )
    return commands


def run_commands(commands: list[ValidationCommand], *, dry_run: bool) -> int:
    for command in commands:
        print(f"[{command.name}] {command.shell_line()}")
        if dry_run:
            continue
        result = subprocess.run(command.argv, cwd=ROOT, check=False)
        if result.returncode != 0:
            print(f"[{command.name}] failed with exit code {result.returncode}")
            return result.returncode
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tier", choices=("quick", "standard", "full"), help="Validation tier to run.")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without executing them.")
    args = parser.parse_args()
    return run_commands(build_commands(args.tier), dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
