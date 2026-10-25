"""UDP echo on a port, logging each sender. Used to check the firewall rule from another machine."""
import socket
import sys

port = int(sys.argv[1])
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(("0.0.0.0", port))
print(f"echo listening on {port}", flush=True)
while True:
    data, addr = s.recvfrom(2048)
    print(f"from {addr[0]}:{addr[1]} {data!r}", flush=True)
    s.sendto(b"echo:" + data, addr)
