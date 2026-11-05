#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
export P2R_DOCKER_TESTS=1
uv run --extra test python -m pytest -q tests/unit/autonomous/test_execution.py tests/integration/autonomous/test_execution_docker.py
# Optional actual OpenSSH test (test-owned keys/known_hosts only):
# docker build -t paper2run-sshd-test:1 -f scripts/autonomous/sshd-test.Dockerfile scripts/autonomous
# P2R_SSH_TESTS=1 uv run --extra test python -m pytest -q tests/integration/autonomous/test_execution_ssh.py
