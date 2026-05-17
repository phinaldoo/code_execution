#!/usr/bin/env bash
set -euo pipefail

EXAMPLE_FILE=".env.example"
TARGET_FILE=".env"

printf 'Setting up code execution gateway configuration...\n\n'

generate_api_secret() {
  local python_cmd
  local secret

  for python_cmd in python3 python; do
    if command -v "$python_cmd" >/dev/null 2>&1; then
      if secret="$("$python_cmd" - <<'PY' 2>/dev/null
import secrets

print(secrets.token_hex(32))
PY
      )"; then
        printf '%s\n' "$secret"
        return 0
      fi
    fi
  done

  if command -v py >/dev/null 2>&1; then
    if secret="$(py -3 - <<'PY' 2>/dev/null
import secrets

print(secrets.token_hex(32))
PY
    )"; then
      printf '%s\n' "$secret"
      return 0
    fi
  fi

  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
    return 0
  fi

  printf 'Python 3 or openssl is required to generate API_KEYS. Install one and retry.\n' >&2
  return 1
}

escape_sed_replacement() {
  local value="$1"
  value=${value//\\/\\\\}
  value=${value//&/\\&}
  value=${value//|/\\|}
  printf '%s' "$value"
}

sed_in_place() {
  local file="$1"
  local expression="$2"
  local tmp
  tmp="$(mktemp "${file}.XXXXXX")"

  trap 'rm -f "$tmp"' RETURN
  sed "$expression" "$file" >"$tmp"
  mv "$tmp" "$file"
  trap - RETURN
}

ensure_default_api_keys() {
  local target_file="$1"
  local current_value=""

  if grep -q '^API_KEYS=' "$target_file"; then
    current_value="$(grep -m1 '^API_KEYS=' "$target_file" | sed 's/^API_KEYS=//')"
    current_value="${current_value%\"}"
    current_value="${current_value#\"}"
    current_value="${current_value%\'}"
    current_value="${current_value#\'}"
  else
    printf 'API_KEYS=\n' >>"$target_file"
  fi

  local secret_part="$current_value"
  if [[ "$secret_part" == *':'* ]]; then
    secret_part="${secret_part#*:}"
  fi

  local normalized_value
  normalized_value="$(printf '%s' "$current_value" | tr '[:upper:]' '[:lower:]')"

  case "$normalized_value" in
    ''|changeme|default|local:changeme|local:default|replace-with-a-long-random-secret|local:replace-with-a-long-random-secret)
      ;;
    *)
      if [ "${#secret_part}" -ge 32 ]; then
        printf 'API_KEYS already configured\n'
        return 0
      fi
      ;;
  esac

  local secret
  secret="$(generate_api_secret)"
  if [ -z "$secret" ]; then
    printf 'Failed to generate API_KEYS\n' >&2
    return 1
  fi

  secret="$(escape_sed_replacement "$secret")"
  sed_in_place "$target_file" "s|^API_KEYS=.*|API_KEYS=local:${secret}|"
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
  printf 'Missing %s; cannot create setup configuration.\n' "$EXAMPLE_FILE" >&2
  exit 1
fi

if [ ! -f "$TARGET_FILE" ]; then
  cp "$EXAMPLE_FILE" "$TARGET_FILE"
  printf 'Created %s from %s\n' "$TARGET_FILE" "$EXAMPLE_FILE"
else
  printf '%s already exists; syncing new keys from %s\n' "$TARGET_FILE" "$EXAMPLE_FILE"
  sync_env_with_example "$EXAMPLE_FILE" "$TARGET_FILE"
fi

ensure_default_api_keys "$TARGET_FILE"

cat <<'EONEXT'

Setup complete.

Next steps:
  1. Review .env if you want to adjust ports, CORS, limits, or production hardening.
  2. Start the gateway: make up
  3. Check status: make ps
EONEXT
