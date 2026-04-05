#!/usr/bin/env bash
set -euo pipefail

EXAMPLE_FILE=".env.example"
TARGET_FILE=".env"

sync_env_with_example() {
  local example_file="$1"
  local target_file="$2"
  local added=0
  local appended_any=0

  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      ''|'#'*) continue ;;
    esac

    if [[ "$line" != *'='* ]]; then
      continue
    fi

    local key="${line%%=*}"
    key="${key%%[[:space:]]*}"
    key="${key##[[:space:]]*}"

    if [ -z "$key" ]; then
      continue
    fi

    if ! grep -Fq "${key}=" "$target_file" 2>/dev/null; then
      if [ "$appended_any" -eq 0 ]; then
        if [ -s "$target_file" ] && [ "$(tail -c1 "$target_file" 2>/dev/null || true)" != $'\n' ]; then
          printf '\n' >> "$target_file"
        fi
        appended_any=1
      fi
      printf '%s\n' "$line" >> "$target_file"
      added=$((added + 1))
    fi
  done < "$example_file"

  if [ "$added" -gt 0 ]; then
    printf 'Added %d new key(s) from %s into %s\n' "$added" "$example_file" "$target_file"
  fi
}

if [ ! -f "$EXAMPLE_FILE" ]; then
  printf 'No %s found\n' "$EXAMPLE_FILE"
  exit 0
fi

if [ ! -f "$TARGET_FILE" ]; then
  cp "$EXAMPLE_FILE" "$TARGET_FILE"
  printf 'Created %s from %s\n' "$TARGET_FILE" "$EXAMPLE_FILE"
else
  sync_env_with_example "$EXAMPLE_FILE" "$TARGET_FILE"
fi
