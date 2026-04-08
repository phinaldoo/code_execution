#!/usr/bin/env bash
set -euo pipefail

EXAMPLE_FILE=".env.example"
TARGET_FILE=".env"

ensure_default_api_keys() {
  local target_file="$1"

  if grep -Eq '^API_KEYS=[^[:space:]].+' "$target_file"; then
    return
  fi

  local secret
  secret="$(openssl rand -hex 32)"
  python3 - "$target_file" "$secret" <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
secret = sys.argv[2]
lines = path.read_text(encoding="utf-8").splitlines()
updated = []
replaced = False

for line in lines:
    if line.startswith("API_KEYS="):
        updated.append(f"API_KEYS=local:{secret}")
        replaced = True
    else:
        updated.append(line)

if not replaced:
    updated.append(f"API_KEYS=local:{secret}")

path.write_text("\n".join(updated) + "\n", encoding="utf-8")
PY
  printf 'Generated a local API_KEYS secret in %s\n' "$target_file"
}

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

ensure_default_api_keys "$TARGET_FILE"
