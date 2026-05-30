#!/usr/bin/env bash
# Razor-Rooster bootstrap: do every initialization step that doesn't need
# credentials, run every pipeline stage that the available credentials and
# data permit, and clearly report what's still required from the operator.
#
# Idempotent: safe to re-run. Each step decides for itself whether to skip,
# run, or block based on observed state.
#
# Strictly descriptive — this script only invokes existing CLI subcommands
# and reports observed state. It never edits config, never writes
# credentials, and never invents values.

set -euo pipefail

# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

VENV_PY="$REPO_ROOT/.venv/bin/razor-rooster"
DB_PATH="${RAZOR_ROOSTER_DB:-$REPO_ROOT/data/trough.duckdb}"
LOG_DIR="$REPO_ROOT/data/logs"
REPORT_DIR="$REPO_ROOT/data/reports"
mkdir -p "$LOG_DIR" "$REPORT_DIR"

BOOTSTRAP_TS="$(date -u +%Y%m%dT%H%M%SZ)"
SUMMARY_JSON="$LOG_DIR/bootstrap-${BOOTSTRAP_TS}.json"
BOOTSTRAP_START_EPOCH="$(date +%s)"

# Auto-load .env when present — operators usually keep credentials there.
# Never overrides values the operator already set in the live environment.
if [[ -f "$REPO_ROOT/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$REPO_ROOT/.env"
    set +a
    DOTENV_LOADED=1
else
    DOTENV_LOADED=0
fi

# Color helpers (turn off when the terminal isn't a tty).
if [[ -t 1 ]]; then
    BOLD=$'\033[1m'
    DIM=$'\033[2m'
    GREEN=$'\033[32m'
    YELLOW=$'\033[33m'
    RED=$'\033[31m'
    BLUE=$'\033[34m'
    RESET=$'\033[0m'
else
    BOLD="" DIM="" GREEN="" YELLOW="" RED="" BLUE="" RESET=""
fi

# Step / blocked / OK markers. Numeric counters are tallied at exit.
RAN_COUNT=0
SKIPPED_COUNT=0
BLOCKED_COUNT=0
declare -a BLOCKED_REASONS=()
declare -a STEP_TIMINGS=()  # entries: "label\tseconds\tstatus"

step()    { echo "${BOLD}${BLUE}▶${RESET} ${BOLD}$*${RESET}"; }
ok()      { echo "  ${GREEN}✓${RESET} $*"; RAN_COUNT=$((RAN_COUNT + 1)); }
skip()    { echo "  ${DIM}-${RESET} $*"; SKIPPED_COUNT=$((SKIPPED_COUNT + 1)); }
blocked() {
    echo "  ${YELLOW}!${RESET} $*"
    BLOCKED_COUNT=$((BLOCKED_COUNT + 1))
    BLOCKED_REASONS+=("$*")
}
fail()    { echo "  ${RED}✗${RESET} $*" >&2; exit 1; }

# Run a CLI command, log its output to a step-specific file under data/logs/,
# and surface success/failure inline with timing.
run_step() {
    local label="$1"; shift
    local logfile="$LOG_DIR/$(date -u +%Y%m%dT%H%M%SZ)-${label// /-}.log"
    local started ended duration status
    started=$(date +%s)
    if "$@" >"$logfile" 2>&1; then
        ended=$(date +%s)
        duration=$((ended - started))
        ok "$label (${duration}s; log: ${logfile#$REPO_ROOT/})"
        STEP_TIMINGS+=("${label}|${duration}|ok")
        status=0
    else
        local rc=$?
        ended=$(date +%s)
        duration=$((ended - started))
        echo "  ${RED}✗${RESET} $label exited $rc after ${duration}s; tail of log:" >&2
        tail -n 20 "$logfile" >&2
        STEP_TIMINGS+=("${label}|${duration}|failed")
        return $rc
    fi
}

env_present() {
    # Returns 0 when the named env var is set and non-empty.
    local var="$1"
    [[ -n "${!var:-}" ]]
}

# ---------------------------------------------------------------------------
# 0. Sanity: venv + CLI present
# ---------------------------------------------------------------------------

step "0. Sanity check"
if [[ ! -x "$VENV_PY" ]]; then
    fail "razor-rooster not found at $VENV_PY. Activate or rebuild the venv first (e.g. \`uv sync\` / \`pip install -e .\`)."
fi
ok "razor-rooster CLI: $VENV_PY"
ok "DB path: $DB_PATH"
ok "Logs dir: ${LOG_DIR#$REPO_ROOT/}"
if (( DOTENV_LOADED )); then
    ok ".env loaded (live env vars take precedence)"
else
    skip ".env not present (copy .env.example to .env to opt in)"
fi

# Convenience: shorten the CLI path for the rest of the script.
RR=("$VENV_PY")

# ---------------------------------------------------------------------------
# 1. Schema bootstrap (always safe; idempotent)
# ---------------------------------------------------------------------------

step "1. Schema bootstrap"
run_step "ingest init" "${RR[@]}" ingest init --db "$DB_PATH"

# ---------------------------------------------------------------------------
# 2. Ingest credential detection
# ---------------------------------------------------------------------------

step "2. Detect available data-ingest credentials"

# Schema mirror of src/razor_rooster/data_ingest/credentials.py
# (no-auth sources — World Bank, GDELT, USGS, federal_register — are absent).
declare -a INGEST_SOURCES_AVAILABLE=()
declare -a INGEST_SOURCES_BLOCKED=()

# Helper: declare an env-key-driven source.
check_api_key_source() {
    local source_id="$1"
    local var="$2"
    if env_present "$var"; then
        ok "$source_id ($var present)"
        INGEST_SOURCES_AVAILABLE+=("$source_id")
    else
        blocked "$source_id requires $var (not set)"
        INGEST_SOURCES_BLOCKED+=("$source_id:$var")
    fi
}

check_user_pass_source() {
    local source_id="$1"
    local user_var="$2"
    local pass_var="$3"
    if env_present "$user_var" && env_present "$pass_var"; then
        ok "$source_id ($user_var + $pass_var present)"
        INGEST_SOURCES_AVAILABLE+=("$source_id")
    else
        local missing=""
        env_present "$user_var" || missing="$user_var"
        env_present "$pass_var" || missing="${missing:+$missing, }$pass_var"
        blocked "$source_id requires $missing"
        INGEST_SOURCES_BLOCKED+=("$source_id:$missing")
    fi
}

check_api_key_source "fred"            "FRED_API_KEY"
check_user_pass_source "acled"         "ACLED_USERNAME" "ACLED_PASSWORD"
check_api_key_source "eia"             "EIA_API_KEY"
check_api_key_source "nrc_adams"       "NRC_ADAMS_API_KEY"
check_api_key_source "regulations_gov" "REGULATIONS_GOV_API_KEY"
check_api_key_source "noaa"            "NOAA_CDO_TOKEN"

# No-auth sources always run (when scheduled).
INGEST_SOURCES_AVAILABLE+=("worldbank")
INGEST_SOURCES_AVAILABLE+=("gdelt")
INGEST_SOURCES_AVAILABLE+=("usgs")
INGEST_SOURCES_AVAILABLE+=("federal_register")
ok "no-auth sources always available: worldbank, gdelt, usgs, federal_register"

# ---------------------------------------------------------------------------
# 3. Ingest cycle (run when at least one source is available)
# ---------------------------------------------------------------------------

step "3. Run ingest cycle"
if (( ${#INGEST_SOURCES_AVAILABLE[@]} == 0 )); then
    skip "no usable sources detected — schema is still applied"
else
    # Comma-join the available source list for --source.
    SOURCE_CSV="$(IFS=,; echo "${INGEST_SOURCES_AVAILABLE[*]}")"
    if run_step "ingest cycle (sources: $SOURCE_CSV)" \
        "${RR[@]}" ingest cycle \
            --db "$DB_PATH" \
            --source "$SOURCE_CSV" \
            --quiet; then
        true
    else
        blocked "ingest cycle returned non-zero — see log; dependent stages may still run on prior data if any exists"
    fi
fi

run_step "ingest status" "${RR[@]}" ingest status --db "$DB_PATH" || true

# ---------------------------------------------------------------------------
# 4. Pattern library — refresh
# ---------------------------------------------------------------------------

step "4. Pattern library refresh"
# Pattern library reads from already-ingested rows; safe to run idempotently
# even on an empty DB (it just emits zero classes).
if run_step "pattern-library refresh" \
    "${RR[@]}" pattern-library refresh --db "$DB_PATH"; then
    true
else
    blocked "pattern-library refresh returned non-zero"
fi

# ---------------------------------------------------------------------------
# 5. Polymarket — sync (operator must have ack'd ToS first)
# ---------------------------------------------------------------------------

step "5. Polymarket connector"
# Probe the ToS-ack state by parsing `polymarket status` output.
POLY_STATUS_OUT="$("${RR[@]}" polymarket status --db "$DB_PATH" 2>&1 || true)"
if echo "$POLY_STATUS_OUT" | grep -qiE "tos.*ack(nowledged)?[: ]+yes|tos_acknowledged[:= ]*true"; then
    ok "Polymarket ToS already acknowledged"
    if run_step "polymarket sync" "${RR[@]}" polymarket sync --db "$DB_PATH"; then
        true
    else
        blocked "polymarket sync returned non-zero — see log"
    fi
elif [[ "${RAZORROO_AUTOACK_POLYMARKET:-}" == "1" ]]; then
    # Operator pre-opted into non-interactive ack via env var. Use --yes.
    if run_step "polymarket ack-tos --yes (via RAZORROO_AUTOACK_POLYMARKET=1)" \
        "${RR[@]}" polymarket ack-tos --db "$DB_PATH" --yes; then
        if run_step "polymarket sync" "${RR[@]}" polymarket sync --db "$DB_PATH"; then
            true
        else
            blocked "polymarket sync returned non-zero — see log"
        fi
    else
        blocked "polymarket ack-tos --yes failed; sync skipped"
    fi
else
    blocked "Polymarket ToS not acknowledged. Run \`razor-rooster polymarket ack-tos\` once (interactive), or re-run this script with RAZORROO_AUTOACK_POLYMARKET=1."
fi

# ---------------------------------------------------------------------------
# 6. Kalshi — sync (operator must have ack'd ToS + be in allow-list)
# ---------------------------------------------------------------------------

step "6. Kalshi connector"
KALSHI_STATUS_OUT="$("${RR[@]}" kalshi status --db "$DB_PATH" 2>&1 || true)"
if echo "$KALSHI_STATUS_OUT" | grep -qiE "tos.*ack(nowledged)?[: ]+yes|tos_acknowledged[:= ]*true"; then
    ok "Kalshi ToS already acknowledged"
    if run_step "kalshi sync" "${RR[@]}" kalshi sync --db "$DB_PATH"; then
        true
    else
        blocked "kalshi sync returned non-zero — see log"
    fi
elif [[ "${RAZORROO_AUTOACK_KALSHI:-}" == "1" ]]; then
    if run_step "kalshi ack-tos --yes (via RAZORROO_AUTOACK_KALSHI=1)" \
        "${RR[@]}" kalshi ack-tos --db "$DB_PATH" --yes; then
        if run_step "kalshi sync" "${RR[@]}" kalshi sync --db "$DB_PATH"; then
            true
        else
            blocked "kalshi sync returned non-zero — see log"
        fi
    else
        blocked "kalshi ack-tos --yes failed; sync skipped"
    fi
else
    blocked "Kalshi ToS not acknowledged. Run \`razor-rooster kalshi ack-tos\` once (interactive), or re-run with RAZORROO_AUTOACK_KALSHI=1."
fi

# ---------------------------------------------------------------------------
# 7. Signal scanner
# ---------------------------------------------------------------------------

step "7. Signal scanner"
if run_step "scan run" "${RR[@]}" scan run --db "$DB_PATH"; then
    true
else
    blocked "scan run returned non-zero — see log; downstream stages may degrade"
fi

# ---------------------------------------------------------------------------
# 8. Mispricing detector
# ---------------------------------------------------------------------------

step "8. Mispricing detector"
if run_step "mispricing run" "${RR[@]}" mispricing run --db "$DB_PATH"; then
    true
else
    blocked "mispricing run returned non-zero — see log"
fi

# ---------------------------------------------------------------------------
# 9. Position engine — paper-analysis sizing
# ---------------------------------------------------------------------------

step "9. Position engine"
# Position engine refuses to run until the operator declares a bankroll.
# Probe by running it: if it fails with the no-bankroll error and we have
# RAZORROO_BANKROLL_USD set, configure and retry. Never invent a value.
PE_RUN_LOG="$LOG_DIR/$(date -u +%Y%m%dT%H%M%SZ)-position-engine-run-probe.log"
if "${RR[@]}" position-engine run --db "$DB_PATH" >"$PE_RUN_LOG" 2>&1; then
    ok "position-engine run (log: ${PE_RUN_LOG#$REPO_ROOT/})"
elif grep -q "no bankroll_config" "$PE_RUN_LOG"; then
    if [[ -n "${RAZORROO_BANKROLL_USD:-}" ]]; then
        if [[ "$RAZORROO_BANKROLL_USD" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
            if run_step "position-engine config --bankroll $RAZORROO_BANKROLL_USD (via RAZORROO_BANKROLL_USD)" \
                "${RR[@]}" position-engine config \
                    --db "$DB_PATH" \
                    --bankroll "$RAZORROO_BANKROLL_USD" \
                    --no-prompt \
                    --acknowledge-analytical \
                    --notes "set by scripts/bootstrap.sh"; then
                if run_step "position-engine run (after config)" \
                    "${RR[@]}" position-engine run --db "$DB_PATH"; then
                    true
                else
                    blocked "position-engine run still failed after config — see log"
                fi
            else
                blocked "position-engine config returned non-zero — see log"
            fi
        else
            blocked "RAZORROO_BANKROLL_USD ($RAZORROO_BANKROLL_USD) is not a valid USD amount; ignoring"
        fi
    else
        blocked "position-engine has no bankroll declared. Run \`razor-rooster position-engine config --bankroll <usd>\` once, or re-run with RAZORROO_BANKROLL_USD=<amount>."
    fi
else
    echo "  ${RED}✗${RESET} position-engine run failed for an unexpected reason; tail of log:" >&2
    tail -n 20 "$PE_RUN_LOG" >&2
    blocked "position-engine run failed; see log"
fi

# ---------------------------------------------------------------------------
# 10. Monitor
# ---------------------------------------------------------------------------

step "10. Monitor"
if run_step "monitor run" "${RR[@]}" monitor run --db "$DB_PATH"; then
    true
else
    blocked "monitor run returned non-zero — see log"
fi

# ---------------------------------------------------------------------------
# 11. Report (always safe; renders against whatever state exists)
# ---------------------------------------------------------------------------

step "11. Daily report"
TODAY="$(date -u +%Y%m%d)"
MD_PATH="$REPORT_DIR/${TODAY}.md"
HTML_PATH="$REPORT_DIR/${TODAY}.html"
run_step "report generate (markdown + html)" \
    "${RR[@]}" report generate \
        --db "$DB_PATH" \
        --markdown "$MD_PATH" \
        --html "$HTML_PATH" \
        --quiet
ok "report rendered: ${MD_PATH#$REPO_ROOT/}"
ok "report rendered: ${HTML_PATH#$REPO_ROOT/}"

# ---------------------------------------------------------------------------
# 12. Summary
# ---------------------------------------------------------------------------

BOOTSTRAP_END_EPOCH="$(date +%s)"
TOTAL_SECS=$((BOOTSTRAP_END_EPOCH - BOOTSTRAP_START_EPOCH))

echo ""
echo "${BOLD}Bootstrap summary${RESET}"
echo "  ${GREEN}ran:${RESET}     $RAN_COUNT step(s)"
echo "  ${DIM}skipped:${RESET} $SKIPPED_COUNT step(s)"
echo "  ${YELLOW}blocked:${RESET} $BLOCKED_COUNT step(s)"
echo "  ${BOLD}total:${RESET}   ${TOTAL_SECS}s"

if (( ${#STEP_TIMINGS[@]} > 0 )); then
    echo ""
    echo "${BOLD}Per-step timings${RESET}"
    for entry in "${STEP_TIMINGS[@]}"; do
        IFS='|' read -r tlabel tsecs tstatus <<<"$entry"
        if [[ "$tstatus" == "ok" ]]; then
            printf "  %4ds  %s\n" "$tsecs" "$tlabel"
        else
            printf "  %4ds  ${RED}%s${RESET}  (%s)\n" "$tsecs" "$tlabel" "$tstatus"
        fi
    done
fi

if (( ${#BLOCKED_REASONS[@]} > 0 )); then
    echo ""
    echo "${BOLD}Operator action required to lift remaining blocks:${RESET}"
    for reason in "${BLOCKED_REASONS[@]}"; do
        echo "  - $reason"
    done
fi

# Write the structured summary artifact for cron-driven runs.
{
    echo "{"
    echo "  \"kind\": \"bootstrap_summary\","
    echo "  \"timestamp\": \"$BOOTSTRAP_TS\","
    echo "  \"total_seconds\": $TOTAL_SECS,"
    echo "  \"counts\": {\"ran\": $RAN_COUNT, \"skipped\": $SKIPPED_COUNT, \"blocked\": $BLOCKED_COUNT},"
    echo "  \"db_path\": \"$DB_PATH\","
    echo "  \"dotenv_loaded\": $([ "$DOTENV_LOADED" = "1" ] && echo true || echo false),"
    # Per-step timings.
    echo -n "  \"steps\": ["
    first=1
    for entry in "${STEP_TIMINGS[@]}"; do
        IFS='|' read -r tlabel tsecs tstatus <<<"$entry"
        # Escape backslashes and quotes in the label so the JSON stays valid.
        elabel="$(printf '%s' "$tlabel" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"
        if (( first )); then
            first=0
        else
            echo -n ","
        fi
        echo -n " {\"label\": \"$elabel\", \"seconds\": $tsecs, \"status\": \"$tstatus\"}"
    done
    echo " ],"
    # Blocked reasons.
    echo -n "  \"blocked_reasons\": ["
    first=1
    for reason in "${BLOCKED_REASONS[@]}"; do
        ereason="$(printf '%s' "$reason" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"
        if (( first )); then
            first=0
        else
            echo -n ","
        fi
        echo -n " \"$ereason\""
    done
    echo " ]"
    echo "}"
} > "$SUMMARY_JSON"
echo ""
echo "${DIM}structured summary: ${SUMMARY_JSON#$REPO_ROOT/}${RESET}"

# Optional: hand off to `report watch` so the operator gets continuous renders.
if [[ "${RAZORROO_BOOTSTRAP_THEN_WATCH:-}" == "1" ]]; then
    echo ""
    step "13. Hand off to report watch (RAZORROO_BOOTSTRAP_THEN_WATCH=1)"
    HTML_PATH="$REPORT_DIR/latest.html"
    MD_PATH="$REPORT_DIR/latest.md"
    INTERVAL="${RAZORROO_WATCH_INTERVAL:-3600}"
    echo "  ${BLUE}exec:${RESET} report watch --interval $INTERVAL --html $HTML_PATH --markdown $MD_PATH --on-change"
    exec "${RR[@]}" report watch \
        --db "$DB_PATH" \
        --interval "$INTERVAL" \
        --html "$HTML_PATH" \
        --markdown "$MD_PATH" \
        --on-change
fi

# Exit 0 when nothing fatal happened; blocks are not failures.
exit 0
