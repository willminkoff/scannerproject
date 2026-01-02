#!/bin/bash
set -u

CONF="/usr/local/etc/rtl_airband.conf"
OUT="/run/rtl_airband_last_freq.txt"

mkdir -p /run
echo "-" > "$OUT"

JOURNAL_PID=""

start_journal_follow() {
  if command -v journalctl >/dev/null 2>&1; then
    stdbuf -oL -eL journalctl -u rtl-airband -f -n 0 -o cat --no-pager 2>/dev/null \
    | stdbuf -oL awk -v OUT="$OUT" '
      {
        if (match($0, /[0-9]+\.[0-9]+/)) {
          freq = substr($0, RSTART, RLENGTH);
          if (freq != last) {
            last = freq;
            print last > OUT;
            fflush(OUT);
          }
        }
      }
    ' &
    JOURNAL_PID=$!
  fi
}

trap 'if [ -n "${JOURNAL_PID:-}" ]; then kill "$JOURNAL_PID" 2>/dev/null; fi; exit 0' TERM INT

run_airband() {
  # Prefer a pseudo-TTY so rtl_airband emits its scan screen (easier to parse)
  if command -v script >/dev/null 2>&1; then
    script -q -f -c "/usr/local/bin/rtl_airband -f -c $CONF" /dev/null
  else
    /usr/local/bin/rtl_airband -f -c "$CONF"
  fi
}

start_journal_follow

run_airband 2>&1 \
| stdbuf -oL tr "\r" "\n" \
| stdbuf -oL tr -cd "\11\12\40-\176" \
| stdbuf -oL awk -v OUT="$OUT" '
  {
    print;
    if (match($0, /[0-9]+\.[0-9]+/)) {
      freq = substr($0, RSTART, RLENGTH);
      if (freq != last) {
        last = freq;
        print last > OUT;
        fflush(OUT);
      }
    }
  }
'
