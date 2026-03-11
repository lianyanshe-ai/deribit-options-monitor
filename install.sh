#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="$SCRIPT_DIR/deribit-options-monitor"
OPENCLAW_HOME="${OPENCLAW_HOME:-$HOME/.openclaw}"
TARGET_DIR="$OPENCLAW_HOME/workspace/skills/deribit-options-monitor"
TARGET_PARENT="$(dirname "$TARGET_DIR")"

if [[ ! -f "$SOURCE_DIR/SKILL.md" ]]; then
  echo "Install failed: missing $SOURCE_DIR/SKILL.md"
  exit 1
fi

mkdir -p "$TARGET_PARENT"

if [[ -e "$TARGET_DIR" ]]; then
  BACKUP_DIR="${TARGET_DIR}.bak.$(date +%Y%m%d-%H%M%S)"
  mv "$TARGET_DIR" "$BACKUP_DIR"
  echo "Existing skill moved to: $BACKUP_DIR"
fi

if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete --exclude '.cache' --exclude '__pycache__' --exclude '.DS_Store' "$SOURCE_DIR/" "$TARGET_DIR/"
else
  cp -R "$SOURCE_DIR" "$TARGET_DIR"
  rm -rf "$TARGET_DIR/.cache" "$TARGET_DIR/__pycache__" "$TARGET_DIR/.DS_Store"
fi

echo "Installed to: $TARGET_DIR"
echo
echo "Next step:"
echo "python3 \"$TARGET_DIR/__init__.py\" doctor"
