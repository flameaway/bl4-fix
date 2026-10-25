#!/usr/bin/env bash
# Waits for a new Borderlands4.exe to appear, then reports whether it stays alive.
#   tools/watch_game.sh [stay_seconds]
stay=${1:-90}
count() { tasklist //FI "IMAGENAME eq Borderlands4.exe" //NH | grep -c "Borderlands4.exe"; }
before=$(count)

for i in $(seq 1 60); do
    sleep 2
    if [ "$(count)" -gt "$before" ]; then echo "process appeared after ~$((i * 2))s"; break; fi
done
[ "$(count)" -gt "$before" ] || { echo "NO_PROCESS after 120s"; exit 1; }

for i in $(seq 1 $((stay / 2))); do
    sleep 2
    if [ "$(count)" -le "$before" ]; then echo "DIED after ~$((i * 2))s"; exit 2; fi
done
echo "ALIVE after ${stay}s ($(count) running)"
