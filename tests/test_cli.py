from typer.testing import CliRunner

from job_monitor.cli import app


def test_cli_help_uses_product_brand():
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "Job Radar TW" in result.stdout
