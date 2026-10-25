#!/usr/bin/env bash
# Sends one UDP packet from a second machine to each of this PC's addresses and reports whether it was echoed.
#   FWTEST_SSH=user@host FWTEST_LOCAL_LAN=<ip> FWTEST_LOCAL_VPN=<ip> dev/fwtest/probe.sh <port> <label>
port=$1; label=$2
for dst in "$FWTEST_LOCAL_LAN" "$FWTEST_LOCAL_VPN"; do
  result=$(ssh -o BatchMode=yes "$FWTEST_SSH" "python3 -c \"
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(2)
s.sendto(b'$label', ('$dst', $port))
try:
    print('ALLOWED (echo from', s.recvfrom(100)[1][0] + ')')
except socket.timeout:
    print('BLOCKED (no reply)')
\"" 2>&1)
  echo "  $FWTEST_SSH -> $dst:$port  $result"
done
