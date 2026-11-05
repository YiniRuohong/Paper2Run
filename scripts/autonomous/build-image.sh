#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
docker build -t paper2run-experiment:1 -f scripts/autonomous/Dockerfile scripts/autonomous
