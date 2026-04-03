#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_URL="https://github.com/ashwinsathish/AODT-Claude-MCP.git"
BRANCH="main"

# Best-effort self-update on startup:
# - Uses HTTPS directly (avoids SSH/port-22 dependency)
# - Never blocks MCP startup permanently on network/auth issues
if command -v git >/dev/null 2>&1 && [ -d "${SCRIPT_DIR}/.git" ]; then
  if command -v timeout >/dev/null 2>&1; then
    timeout 20s git -C "${SCRIPT_DIR}" pull --ff-only "${REPO_URL}" "${BRANCH}" >/dev/null 2>&1 || true
  else
    git -C "${SCRIPT_DIR}" pull --ff-only "${REPO_URL}" "${BRANCH}" >/dev/null 2>&1 || true
  fi
fi

exec python3 "${SCRIPT_DIR}/mcp_server.py"
