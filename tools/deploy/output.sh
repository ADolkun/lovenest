#!/usr/bin/env bash
# Plain, append-only progress; optional color never carries meaning by itself.
# The caller sets log, starts SECONDS at zero, and reserves fd 3 for progress.
output_init() {
  color_reset='' color_heading='' color_ok='' color_fail=''
  if [[ -t 3 && "${TERM:-dumb}" != dumb && -z "${NO_COLOR+x}" && -z "${CI:-}" ]]; then
    color_reset=$'\033[0m'
    color_heading=$'\033[1;36m'
    color_ok=$'\033[32m'
    color_fail=$'\033[31m'
  fi
  phase='' phase_started=0
  printf '%sLovenest deployment%s\n' "$color_heading" "$color_reset" >&3
}

log_event() {
  printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$log"
}

phase_begin() {
  phase=$1
  phase_started=$SECONDS
  printf '  %-21s ' "$phase" >&3
  log_event "START $phase"
}

phase_end() {
  local outcome=${1:-OK} elapsed=$((SECONDS - phase_started)) color=$color_ok
  [[ "$outcome" == OK ]] || color=$color_fail
  printf '%s%s%s %ss\n' "$color" "$outcome" "$color_reset" "$elapsed" >&3
  log_event "END $phase status=$outcome seconds=$elapsed"
  printf '%s\t%s\t%s\n' "$phase" "$elapsed" "$outcome" >> "${log%.log}.timings.tsv"
  phase=''
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
