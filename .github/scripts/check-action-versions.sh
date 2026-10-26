#!/usr/bin/env bash
#
# Compare every `uses: owner/repo@ref` in .github/workflows against the action's
# latest release, and report the ones that are a major version behind.
#
# Advisory by design: it annotates and writes to the job summary, but always
# exits 0. Dependabot opens the actual bump PRs.
#
# Needs: gh (authenticated via GH_TOKEN).
set -euo pipefail

summary="${GITHUB_STEP_SUMMARY:-/dev/stdout}"
report="action-versions.md"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# Third-party actions only: `./local-action` and `docker://` refs have no
# upstream release to compare against.
grep -rhoE 'uses:[[:space:]]*[A-Za-z0-9._-]+/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*@[A-Za-z0-9._-]+' \
  .github/workflows \
  | sed -E 's/^uses:[[:space:]]*//' \
  | sort -u > "$work/refs"

: > "$work/rows"
: > "$work/stale"

while IFS= read -r ref; do
  [ -n "$ref" ] || continue
  path="${ref%@*}"
  pin="${ref#*@}"
  # owner/repo, dropping any subdirectory (github/codeql-action/upload-sarif).
  repo="$(printf '%s' "$path" | cut -d/ -f1,2)"

  if printf '%s' "$pin" | grep -qE '^[0-9a-f]{40}$'; then
    printf '| `%s` | commit SHA | - | pinned to a commit |\n' "$path" >> "$work/rows"
    continue
  fi

  latest="$(gh api "repos/$repo/releases/latest" --jq '.tag_name' 2>/dev/null || true)"
  if [ -z "$latest" ]; then
    latest="$(gh api "repos/$repo/tags" --jq '.[0].name' 2>/dev/null || true)"
  fi
  if [ -z "$latest" ]; then
    printf '| `%s` | `%s` | unknown | could not query the upstream repo |\n' \
      "$path" "$pin" >> "$work/rows"
    continue
  fi

  pin_major="$(printf '%s' "$pin" | sed -E 's/^v//; s/[^0-9].*$//')"
  latest_major="$(printf '%s' "$latest" | sed -E 's/^v//; s/[^0-9].*$//')"

  if [ -n "$pin_major" ] && [ -n "$latest_major" ] && [ "$pin_major" -lt "$latest_major" ]; then
    printf '| `%s` | `%s` | `%s` | **major version behind** |\n' \
      "$path" "$pin" "$latest" >> "$work/rows"
    printf '%s: %s -> %s\n' "$path" "$pin" "$latest" >> "$work/stale"
    echo "::warning title=Outdated action::$path is pinned to $pin; $latest is current"
  else
    printf '| `%s` | `%s` | `%s` | current |\n' "$path" "$pin" "$latest" >> "$work/rows"
  fi
done < "$work/refs"

{
  echo '## Pinned action versions'
  echo
  echo '| Action | Pinned | Latest | Status |'
  echo '|---|---|---|---|'
  sort "$work/rows"
  echo
  if [ -s "$work/stale" ]; then
    printf '%s action(s) are a major version behind. Advisory only - Dependabot opens the bump PRs.\n' \
      "$(wc -l < "$work/stale" | tr -d ' ')"
  else
    echo 'Every action is on its current major version.'
  fi
} | tee "$report" >> "$summary"
