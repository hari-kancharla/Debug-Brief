"""Smoke-test an already installed wheel in an isolated temporary Git project.

Run: python scripts/verify_wheel.py /path/to/wheel-env/bin/debugbrief
Smoke projects use a temporary directory and are removed on completion.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def verify(executable: Path) -> dict:
    executable = executable.resolve()
    python = executable.parent / "python"
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    with tempfile.TemporaryDirectory(prefix="debugbrief-wheel-smoke-") as directory:
        cwd = Path(directory)

        def run(args, expected=0):
            result = subprocess.run(
                [str(part) for part in args],
                cwd=cwd,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
            )
            if result.returncode != expected:
                raise RuntimeError(f"{args[0]} returned {result.returncode}: {result.stderr}")
            return result

        run(["git", "init", "-q"])
        run(["git", "config", "user.name", "Smoke Test"])
        run(["git", "config", "user.email", "smoke@example.invalid"])
        (cwd / "seed.txt").write_text("seed\n")
        run(["git", "add", "seed.txt"])
        run(["git", "commit", "-qm", "Initialize smoke fixture"])
        run([executable, "--version"])
        run([executable, "start", "Installed wheel verification"])
        run(
            [
                executable,
                "run",
                "--verify",
                "--",
                python,
                "-c",
                'import sys; print("observed failure"); sys.exit(7)',
            ],
            expected=7,
        )
        run([executable, "note", "Observed the nonzero result; now run the passing check."])
        run([executable, "run", "--verify", "--", python, "-c", 'print("passing check")'])
        run([executable, "end", "--format", "all"])
        reports = cwd / ".debugbrief/reports"
        data = json.loads(next(reports.glob("*.json")).read_text())
        assert [item["exit_code"] for item in data["commands"]] == [7, 0]
        assert data["commands"][0]["stdout_preview"].strip() == "observed failure"
        assert data["commands"][1]["is_verification"] is True
        # The custom failure was a different command and remains unresolved.
        assert data["evidence"]["state"] == "unverified"
        assert data["currently_failing"]
        assert all(next(reports.glob("*." + suffix)).is_file() for suffix in ["md", "html", "json"])
        run([executable, "status"])
        return data


if __name__ == "__main__":
    verify(Path(sys.argv[1]))
    print(
        "Installed wheel smoke checks passed: exit codes, output, scope, and report formats."
    )
