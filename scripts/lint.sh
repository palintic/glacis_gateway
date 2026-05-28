#!/bin/bash
set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Collect modified .py files (staged + unstaged)
FILES=$(git diff --name-only HEAD 2>/dev/null | grep '\.py$')

if [ -z "$FILES" ]; then
  echo "No modified Python files."
  exit 0
fi

echo "Modified files:"
echo "$FILES" | sed 's/^/  /'
echo ""

echo "Running black..."
echo "$FILES" | xargs black

echo "Running ruff..."
echo "$FILES" | xargs ruff check --fix

echo "Done."
