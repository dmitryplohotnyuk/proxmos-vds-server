#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
[[ $# == 1 ]] || { echo "Usage: $0 OUTPUT.tar.gz" >&2; exit 1; }
output=$1
cd "$PROJECT_DIR"
[[ $(git rev-parse --show-toplevel) == "$PROJECT_DIR" ]] || exit 1
manifest=$(mktemp)
trap 'rm -f "$manifest"' EXIT
# Include uncommitted source changes, but never Git-ignored runtime files.
git ls-files -z --cached --others --exclude-standard > "$manifest"
COPYFILE_DISABLE=1 tar --format=ustar --exclude=.git --exclude=.env --exclude=.venv --exclude=__pycache__ \
    --exclude=secrets --exclude=integration-backups --exclude=service-inventory-backups \
    -czf "$output" --null -T "$manifest"
