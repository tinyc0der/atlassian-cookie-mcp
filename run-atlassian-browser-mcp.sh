#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${ROOT_DIR}/.venv"
PYTHON_BIN="${VENV_DIR}/bin/python"
MCP_BIN="${VENV_DIR}/bin/atlassian-browser-mcp"
ENV_FILE="${ATLASSIAN_BROWSER_MCP_ENV:-${ROOT_DIR}/.env}"

if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required but not installed." >&2
  exit 1
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
  uv venv "${VENV_DIR}"
fi

# Check the package and its deps are importable — NOT an exact version. Pinning
# an exact version here meant every version bump forced a reinstall on startup
# (slow, network-dependent: a hang vector). We only reinstall when something is
# actually missing/broken.
if ! "${PYTHON_BIN}" - <<'PY' >/dev/null 2>&1
import importlib.metadata as m
m.version("atlassian-browser-mcp")  # raises if not installed
import mcp_atlassian, requests  # noqa: F401
PY
then
  # Bound the install so a slow/offline network fails fast instead of hanging
  # the MCP startup forever. 600s is generous for a cold editable install.
  timeout 600 uv pip install --python "${PYTHON_BIN}" -e "${ROOT_DIR}" \
    || { echo "atlassian-browser-mcp: dependency install failed or timed out" >&2; exit 1; }
fi

# Startup compatibility assertion: verify the upstream version and patched signatures
"${PYTHON_BIN}" - <<'PY'
from atlassian_browser_mcp_full import assert_upstream_compatibility
assert_upstream_compatibility()
PY

exec "${MCP_BIN}"
