"""Timestamps DlssgSm86 frame-generation feature (re)creations as they're logged (read-only):
    python dev/perf/sm86_watch.py <log.jsonl> [--minutes 20]"""
import argparse
import os
import time

ap = argparse.ArgumentParser()
ap.add_argument("log")
ap.add_argument("--minutes", type=float, default=20)
a = ap.parse_args()
f = open(a.log, encoding="utf-8", errors="replace")
f.seek(0, os.SEEK_END)
end = time.time() + a.minutes * 60
buf = ""
while time.time() < end:
    chunk = f.read()
    if not chunk:
        time.sleep(0.05)
        continue
    buf += chunk
    *lines, buf = buf.split("\n")
    for ln in lines:
        if '"feature_create"' in ln or '"fg_gate_create_feature"' in ln:
            ms = ""
            if '"create_span_ms"' in ln:
                ms = ln.split('"create_span_ms":', 1)[1].split(",", 1)[0]
            print(f"{time.strftime('%H:%M:%S')}.{int(time.time() * 1000) % 1000:03d} "
                  f"{'ngx-create' if 'fg_gate' in ln else 'feature_create'} span_ms={ms}", flush=True)
