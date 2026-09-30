#!/usr/bin/env bash
set -euo pipefail

source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
test_dir="$(dirname "$source_dir")/pixelfin-test"
[[ -d "$test_dir" ]] || { echo "Missing test folder: $test_dir" >&2; exit 1; }

# Excluded paths are protected from deletion as well as copying.
rsync -a --delete \
  --exclude='.git/' --exclude='.agents/' --exclude='.codex/' --exclude='.aws/' \
  --exclude='data/' --exclude='output/' --exclude='cache/' \
  --exclude='.env' --exclude='.env.*' --exclude='*.zip' \
  --exclude='__pycache__/' --exclude='*.py[cod]' --exclude='.DS_Store' \
  --exclude='docker-compose*.yml' --exclude='docker-compose*.yaml' \
  --exclude='compose*.yml' --exclude='compose*.yaml' \
  "$source_dir/" "$test_dir/"

