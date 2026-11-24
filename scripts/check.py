"""Run every repository check with one command.

Local use, the pre-push hook, and CI all call this script, so the three
cannot drift apart.

    uv run --frozen --extra dev python scripts/check.py
    uv run --frozen --extra dev python scripts/check.py --fix
    uv run --frozen --extra dev python scripts/check.py --with-dashboard
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IN_CI = os.environ.get("GITHUB_ACTIONS") == "true"
PY = sys.executable


@dataclass(frozen=True)
class Step:
    name: str
    commands: tuple[tuple[str, ...], ...]
    fix_hint: str
    cwd: Path = ROOT


def _tool(name: str) -> str | None:
    return shutil.which(name)


def build_steps(*, fix: bool, with_dashboard: bool) -> list[Step]:
    uv = _tool("uv") or "uv"
    if fix:
        lint = Step(
            "lint (ruff check --fix)",
            ((PY, "-m", "ruff", "check", "--fix", "."),),
            "Ruff cannot fix these lines. Read each message and fix the code by hand.",
        )
        fmt = Step(
            "format (ruff format)",
            ((PY, "-m", "ruff", "format", "."),),
            "Ruff format failed. Look for a syntax error in the listed file.",
        )
    else:
        lint = Step(
            "lint (ruff check)",
            ((PY, "-m", "ruff", "check", "."),),
            "Run this script with --fix. Fix any line that is still reported by hand.",
        )
        fmt = Step(
            "format (ruff format --check)",
            ((PY, "-m", "ruff", "format", "--check", "."),),
            "Run this script with --fix to format the files.",
        )

    steps = [
        Step(
            "lockfile (uv lock --check)",
            ((uv, "lock", "--check"),),
            "pyproject.toml and uv.lock disagree. Run `uv lock` and commit uv.lock.",
        ),
        lint,
        fmt,
        Step(
            "tests (pytest)",
            ((PY, "-m", "pytest", "-q"),),
            "Fix the code or the test. Do not delete or skip a test to make it pass.",
        ),
        Step(
            "config (monitor validate-config --strict)",
            ((PY, "-m", "job_monitor.cli", "validate-config", "--strict"),),
            "A file in config/ is invalid. The message names the file and the field.",
        ),
    ]

    if with_dashboard:
        npm = _tool("npm") or "npm"
        steps.append(
            Step(
                "dashboard (npm ci && npm run build)",
                ((npm, "ci"), (npm, "run", "build")),
                "The Cloudflare dashboard build failed. Read the npm output above.",
                cwd=ROOT / "cloudflare",
            )
        )
    return steps


def _group_start(title: str) -> None:
    print(f"::group::{title}" if IN_CI else f"\n==> {title}", flush=True)


def _group_end() -> None:
    if IN_CI:
        print("::endgroup::", flush=True)


def run_step(step: Step) -> bool:
    _group_start(step.name)
    ok = True
    for command in step.commands:
        try:
            result = subprocess.run(command, cwd=step.cwd, check=False)
        except FileNotFoundError:
            print(f"Command not found: {command[0]}", flush=True)
            ok = False
            break
        if result.returncode != 0:
            ok = False
            break
    _group_end()
    if not ok:
        message = f"{step.name} failed. {step.fix_hint}"
        print(f"::error::{message}" if IN_CI else f"FAILED: {message}", flush=True)
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run all repository checks.")
    parser.add_argument("--fix", action="store_true", help="apply safe lint fixes and format files")
    parser.add_argument(
        "--with-dashboard",
        action="store_true",
        help="also build the Cloudflare dashboard (needs Node.js)",
    )
    args = parser.parse_args(argv)

    steps = build_steps(fix=args.fix, with_dashboard=args.with_dashboard)
    results = [(step.name, run_step(step)) for step in steps]

    print("\nSummary", flush=True)
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}", flush=True)
    failed = [name for name, ok in results if not ok]
    if failed:
        print(f"\n{len(failed)} of {len(results)} checks failed.", flush=True)
        return 1
    print(f"\nAll {len(results)} checks passed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
