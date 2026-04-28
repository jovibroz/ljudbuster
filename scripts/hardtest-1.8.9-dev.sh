#!/usr/bin/env bash
#
# LjudBuster Hardtest Runner
# Version: 1.0.0
# Date: 2026-04-28
# Changelog:
# - Initial hardtest runner for LjudBuster 1.8.9-dev
# - Tests SVT livepost, normal SVT article, homepage behavior and AB/SR regressions
# - Polls local dev API and prints status, filename and recent history
#

set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8055}"
MAX_POLLS="${MAX_POLLS:-100}"
SLEEP_SEC="${SLEEP_SEC:-2}"

echo "== LjudBuster hardtest =="
echo "BASE_URL=${BASE_URL}"
echo

echo "--- server version check ---"
curl -fsS "${BASE_URL}/" >/tmp/ljudbuster-hardtest-root.html
grep -aoE '[0-9]+\.[0-9]+\.[0-9]+(-dev)?' /tmp/ljudbuster-hardtest-root.html | head -5 || true
echo

submit_job() {
  local label="$1"
  local mode="$2"
  local fmt="$3"
  local url="$4"

  local resp job_id
  resp="$(
    curl -sS -X POST \
      -F "url=${url}" \
      -F "mode=${mode}" \
      -F "out_format=${fmt}" \
      "${BASE_URL}/download"
  )"

  job_id="$(
    printf '%s' "$resp" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("job_id",""))'
  )"

  echo "$job_id"
}

poll_job() {
  local label="$1"
  local job_id="$2"
  local i payload status message filename

  for i in $(seq 1 "${MAX_POLLS}"); do
    payload="$(curl -sS "${BASE_URL}/status/${job_id}")"

    status="$(
      printf '%s' "$payload" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("status",""))'
    )"

    message="$(
      printf '%s' "$payload" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("message",""))'
    )"

    filename="$(
      printf '%s' "$payload" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("filename",""))'
    )"

    printf '[%s] poll=%03d status=%s' "$label" "$i" "$status"
    if [[ -n "$message" ]]; then printf ' message=%q' "$message"; fi
    if [[ -n "$filename" ]]; then printf ' filename=%q' "$filename"; fi
    printf '\n'

    case "$status" in
      success|error|not_found)
        return 0
        ;;
    esac

    sleep "${SLEEP_SEC}"
  done

  echo "[$label] TIMEOUT after $((MAX_POLLS * SLEEP_SEC)) seconds"
  return 0
}

run_case() {
  local expect="$1"
  local label="$2"
  local mode="$3"
  local fmt="$4"
  local url="$5"
  local job_id

  echo
  echo "================================================================"
  echo "CASE: ${label}"
  echo "EXPECT: ${expect}"
  echo "MODE/FORMAT: ${mode}/${fmt}"
  echo "URL: ${url}"
  echo "================================================================"

  job_id="$(submit_job "$label" "$mode" "$fmt" "$url")"

  if [[ -z "$job_id" ]]; then
    echo "[$label] ERROR: no job_id returned"
    return 0
  fi

  echo "[$label] JOB_ID=${job_id}"
  poll_job "$label" "$job_id"
}

# expect|label|mode|format|url
CASES=$(cat <<'CASES_EOF'
success|svt-livepost-trump|video|auto|https://www.svt.se/nyheter/utrikes/senaste-nytt-om-usa-och-gronland?inlagg=ba2f6d4866c04df29faa9f830fdcd120
success|svt-article-venezuela|video|auto|https://www.svt.se/nyheter/utrikes/utlandska-investerare-flockas-kring-venezuelas-olja-svenskt-bolag-med-i-kapplopningen
probe|svt-sport-elfsborg|video|auto|https://www.svt.se/sport/fotboll/elfsborgs-bakslag-tappade-och-forlorade-mot-kalmar
probe|svt-energy-crisis|video|auto|https://www.svt.se/nyheter/utrikes/senaste-nytt-om-den-globala-energikrisen-och-oljan
negative|svt-homepage-should-not-randomly-download|video|auto|https://www.svt.se
regression|aftonbladet-video|video|auto|https://www.aftonbladet.se/nojesbladet/a/d4JGdz/taylor-swift-vill-skydda-sin-rost
regression|sr-audio|audio|m4a|https://www.sverigesradio.se/artikel/brand-stoppar-tagen
CASES_EOF
)

while IFS='|' read -r expect label mode fmt url; do
  [[ -n "${label:-}" ]] || continue
  run_case "$expect" "$label" "$mode" "$fmt" "$url"
done <<< "$CASES"

echo
echo "================================================================"
echo "RECENT HISTORY"
echo "================================================================"
curl -sS "${BASE_URL}/api/history" | python3 - <<'PY'
import sys, json
data = json.load(sys.stdin)
for item in data[:12]:
    print(
        f"{item.get('status','?'):10} "
        f"{item.get('mode','?'):5} "
        f"{item.get('format','?'):6} "
        f"{item.get('filename','') or '-':60} "
        f"{item.get('title','')[:80]}"
    )
PY

echo
echo "================================================================"
echo "DOWNLOADS"
echo "================================================================"
find downloads -maxdepth 1 -type f -printf '%TY-%Tm-%Td %TH:%TM  %s bytes  %f\n' 2>/dev/null | sort | tail -30 || true
