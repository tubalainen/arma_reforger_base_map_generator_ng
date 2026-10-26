#!/usr/bin/env bash
#
# Funnel the scheduled audit's findings into a single open tracking issue, so a
# daily run that nobody is watching still surfaces somewhere. Re-running updates
# the same issue with a comment instead of opening a new one every morning.
#
# Needs: gh (authenticated via GH_TOKEN), REPO, RUN_URL and the three *_RESULT
# job outcomes in the environment; reports/ holds the downloaded artifacts.
set -euo pipefail

TITLE='Daily security audit: findings'
body="$(mktemp)"
trap 'rm -f "$body"' EXIT

status() {
  case "$2" in
    success) printf -- '- %s: clean\n' "$1" ;;
    failure) printf -- '- **%s: findings**\n' "$1" ;;
    *)       printf -- '- %s: %s\n' "$1" "$2" ;;
  esac
}

{
  echo "Scheduled scan of \`$REPO\` reported findings."
  echo
  status 'Python dependency CVEs' "${PY_RESULT:-unknown}"
  status 'Container image CVEs' "${IMG_RESULT:-unknown}"
  status 'Workflow and action audit' "${ACT_RESULT:-unknown}"
  echo
  echo "Full logs and job summaries: $RUN_URL"

  for f in reports/python-cves.md reports/container-cves.txt reports/zizmor.txt; do
    [ -s "$f" ] || continue
    echo
    echo "<details><summary>$(basename "$f")</summary>"
    echo
    echo '```'
    # Keep the issue body under GitHub's 65536-character limit; the run has the
    # complete report.
    head -c 15000 "$f"
    echo
    echo '```'
    echo
    echo '</details>'
  done
} > "$body"

# The label may not exist yet; --force creates or updates it either way.
gh label create security --repo "$REPO" --color d93f0b \
  --description 'Security audit findings' --force >/dev/null 2>&1 || true

number="$(gh issue list --repo "$REPO" --state open --label security \
  --search "$TITLE in:title" --json number --jq '.[0].number')"

if [ -n "$number" ]; then
  gh issue comment "$number" --repo "$REPO" --body-file "$body"
  echo "Updated issue #$number"
else
  gh issue create --repo "$REPO" --title "$TITLE" --label security --body-file "$body"
fi
