#!/usr/bin/env python3
"""
traffic_generator.py - background UDP traffic (creates congestion)

Usage (inside Mininet host "bg", or via the 'bg' command in topology.py):
    python3 traffic_generator.py --mbps 14 --duration 20
    python3 traffic_generator.py --pps 1000 --size 1200 --duration 10

Packets go to server UDP 10000 (NOT the game port), so the controller puts
them in the best-effort queue. The server only counts them.
"""
import argparse
import socket
import time


def main():
    parser = argparse.ArgumentParser(description="UDP background traffic")
    parser.add_argument("--dst", default="10.0.0.10")
    parser.add_argument("--port", type=int, default=10000)
    parser.add_argument("--size", type=int, default=1000, help="UDP payload bytes")
    parser.add_argument("--mbps", type=float, default=0, help="target rate (overrides --pps)")
    parser.add_argument("--pps", type=float, default=100, help="packets per second")
    parser.add_argument("--duration", type=float, default=10)
    args = parser.parse_args()

    if args.size < 1 or args.size > 1400:
        raise SystemExit("--size must be 1..1400")
    pps = args.pps
    if args.mbps > 0:
        pps = args.mbps * 1e6 / 8.0 / args.size
    interval = 1.0 / pps
    payload = b"x" * args.size
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    print("bg traffic: %.0f pkt/s x %d B = %.2f Mbit/s to %s:%d for %.0f s"
          % (pps, args.size, pps * args.size * 8 / 1e6, args.dst, args.port, args.duration),
          flush=True)
    start = time.time()
    next_send = start
    sent = 0
    while True:
        now = time.time()
        if now - start >= args.duration:
            break
        # send everything that is due (catch-up loop keeps the average rate right)
        while next_send <= now:
            try:
                sock.sendto(payload, (args.dst, args.port))
            except OSError:
                pass
            sent += 1
            next_send += interval
        time.sleep(0.001)
    elapsed = time.time() - start
    print("bg traffic done: sent %d packets, %.2f Mbit/s average"
          % (sent, sent * args.size * 8 / elapsed / 1e6), flush=True)


if __name__ == "__main__":
    main()
