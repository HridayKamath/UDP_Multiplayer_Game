#!/usr/bin/env python3
"""
client.py - UDP game client (D1: `python3 client.py p1` still works)

  python3 -u client.py p1
  python3 -u client.py p2 --rate 10 --duration 30 --stats-file /tmp/p2.json

What it does
  * registers with the server (retries until confirmed)
  * sends a position update every 1/rate s with sequence number 0,1,2,...
  * RTT   = time the ack arrives - timestamp it sent (the server echoes the timestamp)
  * jitter (smoothed) = 0.9*jitter + 0.1*|rtt - previous_rtt|
  * detects missing updates of the OTHER players from their sequence numbers
    (downstream loss) - UDP never retransmits, we only detect and count
  * moves by itself (random walk) or by keyboard: the web UI writes
    /tmp/cn_ctl_<id>.json and the client follows it while the file is fresh
"""
import argparse
import json
import os
import random
import signal
import socket
import sys
import threading
import time

from common import PlayerTracker, GAME_PORT, SERVER_IP, is_int, to_float

CTL_FRESH = 0.5          # seconds a control file stays valid


class Client:
    def __init__(self, args):
        self.args = args
        self.pid = args.player_id
        self.server = (args.server, args.port)
        self.session = random.randint(1, 2 ** 31 - 1)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(0.2)
        self.receiver_stop = threading.Event()
        self.quit = threading.Event()
        self.lock = threading.Lock()
        self.ctl_path = "/tmp/cn_ctl_%s.json" % self.pid
        self.x = random.uniform(20, 80)
        self.y = random.uniform(20, 80)
        self.sent = 0
        self.acked = set()
        self.rtts = []
        self.rtt = None
        self.jitter = 0.0
        self.diff_sum = 0.0
        self.diff_count = 0
        self.registered = False
        self.others = {}                 # other player id -> PlayerTracker
        self.updates_seen = 0
        self.last_ack_time = None
        self.start_time = time.time()

    def send(self, obj):
        try:
            self.sock.sendto(json.dumps(obj).encode("utf-8"), self.server)
        except OSError:
            pass

    # ---------- receiving thread ----------
    def receiver(self):
        while not self.receiver_stop.is_set():
            try:
                data, _addr = self.sock.recvfrom(4096)
            except (socket.timeout, OSError):
                continue
            try:
                msg = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(msg, dict):
                continue
            kind = msg.get("type")
            if kind == "ack":
                self.on_ack(msg)
            elif kind == "registered":
                self.registered = True
            elif kind == "update":
                self.on_other_update(msg)
            elif kind == "error":
                print("[%s] server error: %s" % (self.pid, msg.get("msg")), flush=True)

    def on_ack(self, msg):
        seq, t = msg.get("seq"), msg.get("timestamp")
        if msg.get("session") != self.session or not is_int(seq):
            return
        if isinstance(t, bool) or not isinstance(t, (int, float)):
            return
        now = time.time()
        with self.lock:
            if seq in self.acked:                 # duplicate ack
                return
            self.acked.add(seq)
            rtt = (now - t) * 1000.0              # milliseconds
            if rtt < 0:
                return
            if self.rtt is not None:
                diff = abs(rtt - self.rtt)
                self.jitter = 0.9 * self.jitter + 0.1 * diff
                self.diff_sum += diff
                self.diff_count += 1
            self.rtt = rtt
            self.rtts.append(rtt)
            self.last_ack_time = now

    def on_other_update(self, msg):
        """An update of another player, forwarded by the server (D1 behaviour)."""
        other, seq = msg.get("player_id"), msg.get("seq")
        if not isinstance(other, str) or other == self.pid or not is_int(seq):
            return
        session = msg.get("session")
        if not is_int(session):
            session = 0
        with self.lock:
            tracker = self.others.setdefault(other, PlayerTracker())
            tracker.on_packet(session, seq, join_midstream=True)
            self.updates_seen += 1

    def down_totals(self):
        with self.lock:
            return (sum(t.received for t in self.others.values()),
                    sum(t.lost for t in self.others.values()))

    # ---------- statistics ----------
    def summary(self):
        down_recv, down_lost = self.down_totals()
        with self.lock:
            rtts = sorted(self.rtts)
            n = len(rtts)
            acked = len(self.acked)
            sent = self.sent
            return {
                "player": self.pid, "session": self.session,
                "duration_s": round(time.time() - self.start_time, 1),
                "sent": sent, "acked": acked,
                "ack_loss_pct": round(100.0 * (sent - acked) / sent, 2) if sent else 0.0,
                "rtt_avg_ms": round(sum(rtts) / n, 2) if n else None,
                "rtt_min_ms": round(rtts[0], 2) if n else None,
                "rtt_p95_ms": round(rtts[min(n - 1, int(0.95 * n))], 2) if n else None,
                "rtt_max_ms": round(rtts[-1], 2) if n else None,
                "jitter_smoothed_ms": round(self.jitter, 2),
                "jitter_mean_ms": round(self.diff_sum / self.diff_count, 2) if self.diff_count else None,
                "down_received": down_recv, "down_lost": down_lost,
                "down_loss_pct": round(100.0 * down_lost / (down_recv + down_lost), 2)
                if (down_recv + down_lost) else 0.0,
            }

    def print_line(self):
        s = self.summary()
        now = time.time()
        if self.last_ack_time is None:
            link = "NO ACK YET"
        elif now - self.last_ack_time > 2.0:
            link = "NO ACK for %.0fs (blocked or network down?)" % (now - self.last_ack_time)
        else:
            link = "ok"
        rtt = "-" if self.rtt is None else "%.1f" % self.rtt
        print("[%s] sent=%d acked=%d rtt=%sms jitter=%.2fms down_recv=%d down_lost=%d link=%s"
              % (self.pid, s["sent"], s["acked"], rtt, self.jitter,
                 s["down_received"], s["down_lost"], link), flush=True)

    # ---------- movement ----------
    def move(self):
        dx = dy = None
        try:
            if time.time() - os.stat(self.ctl_path).st_mtime < CTL_FRESH:
                with open(self.ctl_path) as f:
                    c = json.load(f)
                dx, dy = to_float(c.get("dx")), to_float(c.get("dy"))
        except (OSError, ValueError):
            pass
        if dx is None:
            if not self.args.auto:
                return
            dx, dy = random.uniform(-2, 2), random.uniform(-2, 2)
        else:
            dx, dy = max(-1, min(1, dx)) * 3, max(-1, min(1, dy)) * 3
        self.x = min(100.0, max(0.0, self.x + dx))
        self.y = min(100.0, max(0.0, self.y + dy))

    # ---------- main loop ----------
    def run(self):
        threading.Thread(target=self.receiver, daemon=True).start()
        interval = 1.0 / self.args.rate
        print("[%s] session=%d server=%s:%d rate=%.0f/s"
              % (self.pid, self.session, self.args.server, self.args.port, self.args.rate), flush=True)
        reg = {"type": "register", "player_id": self.pid, "session": self.session}
        self.send(reg)
        next_send = time.time()
        next_print = time.time() + 2.0
        next_register = time.time() + 1.0
        seq = 0
        while not self.quit.is_set():
            now = time.time()
            if self.args.duration and now - self.start_time >= self.args.duration:
                break
            if now >= next_send:
                self.move()
                msg = {"type": "update", "player_id": self.pid, "session": self.session,
                       "seq": seq, "timestamp": now,
                       "x": round(self.x, 1), "y": round(self.y, 1)}
                if self.rtt is not None:
                    msg["rtt"] = round(self.rtt, 2)
                    msg["jitter"] = round(self.jitter, 2)
                dr, dl = self.down_totals()
                msg["down_recv"], msg["down_lost"] = dr, dl
                self.send(msg)
                seq += 1
                self.sent = seq
                next_send += interval
                if next_send < now - 1.0:          # fell far behind: do not burst
                    next_send = now + interval
            if not self.registered and now >= next_register:
                self.send(reg)
                next_register = now + 1.0
            if now >= next_print:
                if not self.args.quiet:
                    self.print_line()
                next_print = now + 2.0
            time.sleep(max(0.0, min(0.005, next_send - time.time())))
        self.finish()

    def finish(self):
        time.sleep(1.0)                              # grace period for the last acks
        self.send({"type": "bye", "player_id": self.pid})
        self.receiver_stop.set()
        s = self.summary()
        print("[%s] FINAL %s" % (self.pid, json.dumps(s)), flush=True)
        if self.args.stats_file:
            tmp = self.args.stats_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(s, f)
            os.replace(tmp, self.args.stats_file)


def main():
    parser = argparse.ArgumentParser(description="UDP game client")
    parser.add_argument("player_id", nargs="?", default="p1",
                        help="player id, e.g. p1 (letters, digits, _ -; max 16)")
    parser.add_argument("--server", default=SERVER_IP)
    parser.add_argument("--port", type=int, default=GAME_PORT)
    parser.add_argument("--rate", type=float, default=10.0, help="updates per second")
    parser.add_argument("--duration", type=float, default=0, help="seconds (0 = until stopped)")
    parser.add_argument("--stats-file", default="", help="write a JSON summary here when finished")
    parser.add_argument("--no-auto", dest="auto", action="store_false",
                        help="do not random-walk (move only by keyboard control)")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    if args.rate <= 0:
        sys.exit("--rate must be > 0")
    client = Client(args)
    stop = lambda *_: client.quit.set()
    signal.signal(signal.SIGTERM, stop)            # Mininet/the web UI stop us with SIGTERM
    signal.signal(signal.SIGINT, stop)
    client.run()


if __name__ == "__main__":
    main()
