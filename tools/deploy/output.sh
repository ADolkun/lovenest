#!/usr/bin/env bash
# Animated terminal activity with a plain fallback; never invent a percentage.
# The caller sets log, starts SECONDS at zero, and reserves fd 3 for progress.
output_init() {
  color_reset='' color_heading='' color_ok='' color_fail='' progress_animated=false
  if [[ -t 3 && "${TERM:-dumb}" != dumb && -z "${NO_COLOR+x}" && -z "${CI:-}" && "${LOVENEST_PROGRESS:-auto}" != plain ]]; then
    if progress_columns >/dev/null; then
      color_reset=$'\033[0m'
      color_heading=$'\033[1;36m'
      color_ok=$'\033[32m'
      color_fail=$'\033[31m'
      progress_animated=true
    fi
  fi
  phase='' phase_started=0 progress_pid='' progress_kind=''
  progress_parser="$(dirname "${BASH_SOURCE[0]}")/progress.awk"
  printf '%sLovenest deployment%s\n' "$color_heading" "$color_reset" >&3
}

log_event() {
  printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$log"
}

phase_begin() {
  phase=$1
  phase_started=$SECONDS
  progress_kind=${2:-}
  log_event "START $phase"
  if [[ -n "$progress_kind" ]]; then
    if [[ "$progress_animated" == true ]]; then
      progress_draw 0 'starting Docker' || progress_animated=false
    fi
    if [[ "$progress_animated" != true ]]; then
      printf '  %-21s RUNNING\n' "$phase" >&3
    fi
    progress_stop_file="${log%.log}.progress-stop"
    rm -f "$progress_stop_file"
    progress_watch &
    progress_pid=$!
  else
    printf '  %-21s ' "$phase" >&3
  fi
}

phase_end() {
  local outcome=${1:-OK} elapsed=$((SECONDS - phase_started)) color=$color_ok
  progress_stop
  [[ -z "$progress_kind" ]] || printf '  %-21s ' "$phase" >&3
  [[ "$outcome" == OK ]] || color=$color_fail
  printf '%s%s%s %ss\n' "$color" "$outcome" "$color_reset" "$elapsed" >&3
  log_event "END $phase status=$outcome seconds=$elapsed"
  printf '%s\t%s\t%s\n' "$phase" "$elapsed" "$outcome" >> "${log%.log}.timings.tsv"
  phase='' progress_kind=''
}

# Read the actual terminal on every frame, not a stale/untrusted COLUMNS value.
# If sizing is unavailable, use the append-only fallback without cursor controls.
progress_columns() {
  local size columns
  size=$(stty size <&3 2>/dev/null) || return 1
  columns=${size##* }
  case "$columns" in ''|*[!0-9]*) return 1 ;; esac
  [[ ${#columns} -le 4 && "$columns" -ge 2 && "$columns" -le 1000 ]] || return 1
  printf '%s' "$columns"
}

progress_draw() {
  local tick=$1 detail=$2 columns limit elapsed line bar='' position i spinner
  columns=$(progress_columns) || return 1
  limit=$((columns - 1))
  elapsed=$((SECONDS - phase_started))
  if [[ "$columns" -ge 36 ]]; then
    # A constant-size segment bounces in both directions. It is not completion.
    position=$((tick % 18))
    [[ "$position" -le 9 ]] || position=$((18 - position))
    for ((i=0; i<12; i++)); do
      if [[ "$i" -ge "$position" && "$i" -lt $((position + 3)) ]]; then
        bar+='='
      else
        bar+=' '
      fi
    done
    line="  $phase [$bar] ${elapsed}s | $detail"
  else
    # A tiny terminal gets a spinner rather than a wrapped or misleading bar.
    case $((tick % 4)) in 0) spinner='|' ;; 1) spinner='/' ;; 2) spinner='-' ;; 3) spinner='\' ;; esac
    line="$spinner ${elapsed}s"
  fi
  # Every dynamic value above is ASCII/fixed-label text. Leave the last column
  # unused so drawing cannot auto-wrap. Never hide the cursor or change modes.
  printf '\r\033[2K%s%s%s' "$color_heading" "${line:0:limit}" "$color_reset" >&3
}

# Animate four times a second, but parse activity only once a second. Plain
# output remains one real elapsed-time update every five seconds. No ETA.
progress_watch() {
  # This is only a reader. Never inherit the deployer's EXIT cleanup in a child.
  trap - EXIT
  sleeper=''
  trap 'for child in $(jobs -pr); do kill "$child" 2>/dev/null; done; wait; exit 0' HUP INT TERM
  local tick=0 detail='starting Docker' interval=5
  [[ "$progress_animated" != true ]] || interval=0.25
  # The marker also covers stop-before-trap-installation on older Bash.
  while [[ ! -e "$progress_stop_file" ]] && kill -0 "$$" 2>/dev/null; do
    sleep "$interval" >/dev/null 2>&1 3>&- 4>&- &
    sleeper=$!
    wait "$sleeper" || exit 0
    sleeper=''
    [[ ! -e "$progress_stop_file" ]] && kill -0 "$$" 2>/dev/null || exit 0
    if [[ "$progress_animated" != true || $((tick % 4)) == 0 ]]; then
      detail=$(awk -v mode="$progress_kind" -f "$progress_parser" "$log") || detail='waiting for Docker'
    fi
    tick=$((tick + 1))
    if [[ "$progress_animated" == true ]]; then
      if progress_draw "$tick" "$detail"; then continue; fi
      # A disconnected/unqueryable terminal must not leave a partial frame.
      printf '\r\033[2K\n' >&3
      progress_animated=false interval=5
    fi
    printf '    %ss elapsed | %s\n' "$((SECONDS - phase_started))" "$detail" >&3
  done
}

progress_stop() {
  if [[ -n "${progress_pid:-}" ]]; then
    : > "$progress_stop_file"
    kill "$progress_pid" 2>/dev/null || true
    wait "$progress_pid" 2>/dev/null || true
    progress_pid=''
    rm -f "$progress_stop_file"
  fi
  if [[ -n "${progress_kind:-}" && "${progress_animated:-false}" == true ]]; then
    printf '\r\033[2K' >&3
  fi
}

output_total() {
  log_event "TOTAL seconds=$SECONDS status=$1"
  printf 'Total\t%s\t%s\n' "$SECONDS" "$1" >> "${log%.log}.timings.tsv"
}

# Keep a bounded excerpt, but retain recent error lines even when buffered stdout
# arrives after stderr. Avoid repeating lines already in the final eight lines.
failure_excerpt() {
  awk '
    {
      tail[NR % 8] = $0
      if ($0 !~ /^\[[^]]+\] (END|TOTAL) / &&
          tolower($0) ~ /(^|[^[:alnum:]])(error|failed|failure|exception|traceback)([^[:alnum:]]|$)/) {
        errors[count % 4] = $0
        positions[count % 4] = NR
        count++
      }
    }
    END {
      first = NR > 8 ? NR - 7 : 1
      for (i = count > 4 ? count - 4 : 0; i < count; i++)
        if (positions[i % 4] < first) print errors[i % 4]
      for (i = first; i <= NR; i++) print tail[i % 8]
    }
  ' "$1"
}
