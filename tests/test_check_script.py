"""Guard tests: local checks, the pre-push hook, and CI must run the same command."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CHECK_SCRIPT = "scripts/check.py"


def _load_check_module():
    spec = importlib.util.spec_from_file_location("check_script", ROOT / CHECK_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # dataclasses look up the defining module in sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_ci_runs_the_shared_check_script_with_dashboard():
    workflow = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8"))
    commands = [step.get("run", "") for step in workflow["jobs"]["test"]["steps"]]
    matching = [cmd for cmd in commands if CHECK_SCRIPT in cmd]
    assert matching, "test.yml must run scripts/check.py; do not list checks separately"
    assert any("--with-dashboard" in cmd for cmd in matching), (
        "CI must build the Cloudflare dashboard: pass --with-dashboard"
    )


def test_pre_push_hook_runs_the_shared_check_script():
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hooks = [hook for repo in config["repos"] for hook in repo["hooks"]]
    pre_push = [hook for hook in hooks if "pre-push" in hook.get("stages", [])]
    assert any(CHECK_SCRIPT in hook["entry"] for hook in pre_push), (
        "the pre-push hook must run scripts/check.py"
    )


def test_check_script_covers_every_ci_check():
    module = _load_check_module()
    names = [step.name for step in module.build_steps(fix=False, with_dashboard=True)]
    for required in ("lockfile", "lint", "format", "tests", "config", "dashboard"):
        assert any(name.startswith(required) for name in names), f"missing check: {required}"


def test_dashboard_step_is_opt_in():
    module = _load_check_module()
    names = [step.name for step in module.build_steps(fix=False, with_dashboard=False)]
    assert not any(name.startswith("dashboard") for name in names)


def test_contributor_docs_point_to_the_shared_check_script():
    for doc in ("AGENTS.md", "CONTRIBUTING.md"):
        text = (ROOT / doc).read_text(encoding="utf-8")
        assert CHECK_SCRIPT in text, f"{doc} must tell contributors to run {CHECK_SCRIPT}"
