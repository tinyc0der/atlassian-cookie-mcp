"""Tests for the MCP launcher env-file handling."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_executable(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content).lstrip())
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _make_launcher_fixture(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    bin_dir = root / ".venv" / "bin"
    bin_dir.mkdir(parents=True)

    script = root / "run-atlassian-browser-mcp.sh"
    shutil.copy2(REPO_ROOT / "run-atlassian-browser-mcp.sh", script)

    _write_executable(
        bin_dir / "python",
        """\
        #!/usr/bin/env bash
        cat >/dev/null
        exit 0
        """,
    )
    _write_executable(
        bin_dir / "atlassian-browser-mcp",
        """\
        #!/usr/bin/env bash
        printf 'JIRA_URL=%s\\n' "${JIRA_URL-}"
        printf 'CONFLUENCE_URL=%s\\n' "${CONFLUENCE_URL-}"
        printf 'TOOLSETS=%s\\n' "${TOOLSETS-}"
        """,
    )

    return root


def _env_with_fake_uv(tmp_path: Path) -> dict[str, str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "uv", "#!/usr/bin/env bash\nexit 0\n")
    return {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    }


def test_launcher_loads_project_dotenv_from_script_directory(tmp_path):
    root = _make_launcher_fixture(tmp_path)
    (root / ".env").write_text(
        "\n".join([
            "JIRA_URL=https://jira.example.com",
            'CONFLUENCE_URL="https://confluence.example.com"',
            "TOOLSETS=jira,confluence",
            "",
        ])
    )

    result = subprocess.run(
        [str(root / "run-atlassian-browser-mcp.sh")],
        check=True,
        cwd=tmp_path,
        env=_env_with_fake_uv(tmp_path),
        text=True,
        capture_output=True,
    )

    assert "JIRA_URL=https://jira.example.com" in result.stdout
    assert "CONFLUENCE_URL=https://confluence.example.com" in result.stdout
    assert "TOOLSETS=jira,confluence" in result.stdout


def test_launcher_allows_custom_env_file_path(tmp_path):
    root = _make_launcher_fixture(tmp_path)
    custom_env = tmp_path / "atlassian-browser-mcp.env"
    custom_env.write_text(
        "\n".join([
            "JIRA_URL=https://custom-jira.example.com",
            "CONFLUENCE_URL=https://custom-confluence.example.com",
            "",
        ])
    )
    env = _env_with_fake_uv(tmp_path)
    env["ATLASSIAN_BROWSER_MCP_ENV"] = str(custom_env)

    result = subprocess.run(
        [str(root / "run-atlassian-browser-mcp.sh")],
        check=True,
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )

    assert "JIRA_URL=https://custom-jira.example.com" in result.stdout
    assert "CONFLUENCE_URL=https://custom-confluence.example.com" in result.stdout
