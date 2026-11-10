#!/usr/bin/env python3
"""
server.py - UDP game server (runs on Mininet host "server", 10.0.0.10)

D1 behaviour kept: players register, send position updates, the server forwards
every update to the other players.
D2 additions:
  * ack for every update (client measures RTT from the echoed timestamp)
  * per-player sequence tracking: lost / duplicate / out-of-order / restarts
  * disconnect detection (no packet for 3 s)
  * background sink on UDP 10000 (counts congestion traffic)
  * writes everything the web UI needs to a JSON state file (atomic write)
"""
import argparse
import json
import os
import signal
import socket
import threading
import time

from common import (GAME_PORT, BG_PORT, PlayerTracker, clean_id, to_float, is_int)

DISCONNECT_AFTER = 3.0      # seconds without packets -> "disconnected"
REMOVE_AFTER = 300.0        # seconds without packets -> forgotten
HOUSEKEEP_EVERY = 0.2
WRITE_EVERY = 0.5


class GameServer:
    def __init__(self, port, bg_port, state_file, quiet):
        self.port = port
        self.bg_port = bg_port
        self.state_file = state_file
        self.quiet = quiet
        self.players = {}
        self.bad_packets = 0
        self.total_packets = 0
        self.bg = {"packets": 0, "bytes": 0, "first": None, "last": None}
        self.bg_prev = (time.time(), 0)
        self.bg_rate_now = 0.0
        self.running = True
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", port))
        self.sock.settimeout(0.05)

    def log(self, text):
        if not self.quiet:
            print(time.strftime("%H:%M:%S"), text, flush=True)

    # ---------- background sink (UDP 10000): only counts ----------
    def bg_loop(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        s.bind(("0.0.0.0", self.bg_port))
        while True:
            try:
                data, _addr = s.recvfrom(65535)
            except OSError:
                return
            now = time.time()
            self.bg["packets"] += 1
            self.bg["bytes"] += len(data)
            if self.bg["first"] is None:
                self.bg["first"] = now
            self.bg["last"] = now

    # ---------- helpers ----------
    def send(self, obj, addr):
        try:
            self.sock.sendto(json.dumps(obj).encode("utf-8"), addr)
        except OSError:
            pass

    def is_connected(self, p, now):
        return (not p["left"]) and now - p["last_seen"] < DISCONNECT_AFTER

    def get_player(self, pid, addr):
        """Return the record (created if new). None = id is in use from another IP."""
        now = time.time()
        p = self.players.get(pid)
        if p is not None and p["ip"] != addr[0] and self.is_connected(p, now):
            return None
        if p is None:
            p = {"id": pid, "tracker": PlayerTracker(), "x": 0.0, "y": 0.0, "seq": -1,
                 "rtt": None, "jitter": None, "down_recv": 0, "down_lost": 0,
                 "recent": [], "last_seen": now, "left": False, "was_connected": False,
                 "ip": addr[0], "port": addr[1], "registered_at": now}
            self.players[pid] = p
        p["ip"], p["port"] = addr[0], addr[1]
        return p

    # ---------- message handling ----------
    def handle(self, data, addr):
        self.total_packets += 1
        try:
            msg = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self.bad_packets += 1
            return
        if not isinstance(msg, dict):
            self.bad_packets += 1
            return
        pid = clean_id(msg.get("player_id"))
        kind = msg.get("type")
        if pid is None:
            self.bad_packets += 1
            return
        p = self.get_player(pid, addr)
        if p is None:
            self.send({"type": "error", "msg": "player_id %s already in use" % pid}, addr)
            return
        now = time.time()

        if kind == "register":
            p["last_seen"] = now
            p["left"] = False
            self.log("register %s from %s:%d" % (pid, addr[0], addr[1]))
            self.send({"type": "registered", "player_id": pid}, addr)

        elif kind == "update":
            session, seq = msg.get("session"), msg.get("seq")
            if not is_int(seq) or seq < 0:
                self.bad_packets += 1
                return
            if session is None:
                session = 0            # D1 clients have no session id
            if not is_int(session):
                self.bad_packets += 1
                return
            p["last_seen"] = now
            p["left"] = False
            p["recent"].append(now)
            outcome = p["tracker"].on_packet(session, seq)
            # ack everything, even duplicates, so the client can measure RTT
            self.send({"type": "ack", "player_id": pid, "session": session, "seq": seq,
                       "timestamp": msg.get("timestamp")}, addr)
            if outcome != "new":
                return               # old/duplicate data must not overwrite the game state
            p["x"], p["y"], p["seq"] = to_float(msg.get("x")), to_float(msg.get("y")), seq
            if msg.get("rtt") is not None:
                p["rtt"] = to_float(msg.get("rtt"))
                p["jitter"] = to_float(msg.get("jitter"))
            if is_int(msg.get("down_recv")) and is_int(msg.get("down_lost")):
                p["down_recv"], p["down_lost"] = msg["down_recv"], msg["down_lost"]
            # D1 behaviour: forward the update to every other connected player
            msg["received_at"] = now
            out = json.dumps(msg).encode("utf-8")
            for oid, o in self.players.items():
                if oid != pid and self.is_connected(o, now):
                    try:
                        self.sock.sendto(out, (o["ip"], o["port"]))
                    except OSError:
                        pass

        elif kind == "bye":
            p["left"] = True
            self.log("bye from %s" % pid)
        else:
            self.bad_packets += 1

    # ---------- periodic ----------
    def housekeeping(self):
        now = time.time()
        for pid in list(self.players):
            p = self.players[pid]
            p["recent"] = [t for t in p["recent"] if now - t <= 1.0]
            connected = self.is_connected(p, now)
            if connected and not p["was_connected"]:
                self.log("player %s connected" % pid)
            if (not connected) and p["was_connected"]:
                self.log("player %s DISCONNECTED" % pid)
            p["was_connected"] = connected
            if now - p["last_seen"] > REMOVE_AFTER:
                del self.players[pid]

    def write_state(self):
        now = time.time()
        interval = now - self.bg_prev[0]
        if interval > 0:
            self.bg_rate_now = (self.bg["bytes"] - self.bg_prev[1]) * 8.0 / interval / 1e6
        self.bg_prev = (now, self.bg["bytes"])
        bg_avg = 0.0
        if self.bg["first"] is not None and self.bg["last"] > self.bg["first"]:
            bg_avg = self.bg["bytes"] * 8.0 / (self.bg["last"] - self.bg["first"]) / 1e6
        players = []
        for pid in sorted(self.players):
            p = self.players[pid]
            t = p["tracker"]
            players.append({
                "id": pid, "ip": p["ip"],
                "status": "connected" if self.is_connected(p, now) else "disconnected",
                "x": round(p["x"], 1), "y": round(p["y"], 1), "seq": p["seq"],
                "up_received": t.received, "up_lost": t.lost,
                "up_loss_pct": round(t.loss_pct(), 2),
                "duplicates": t.duplicates, "out_of_order": t.out_of_order,
                "restarts": t.restarts, "session": t.session,
                "down_received": p["down_recv"], "down_lost": p["down_lost"],
                "rtt_ms": None if p["rtt"] is None else round(p["rtt"], 2),
                "jitter_ms": None if p["jitter"] is None else round(p["jitter"], 2),
                "pps": len(p["recent"]),
                "last_seen_age": round(now - p["last_seen"], 1),
            })
        state = {
            "server_time": now, "game_port": self.port,
            "total_packets": self.total_packets, "bad_packets": self.bad_packets,
            "players": players,
            "bg": {"packets": self.bg["packets"], "bytes": self.bg["bytes"],
                   "rate_mbps_now": round(self.bg_rate_now, 2), "avg_mbps": round(bg_avg, 2)},
        }
        tmp = self.state_file + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.chmod(tmp, 0o644)
            os.replace(tmp, self.state_file)       # atomic: readers never see half a file
        except OSError as e:
            self.log("cannot write state file: %s" % e)

    def run(self):
        threading.Thread(target=self.bg_loop, daemon=True).start()
        self.log("game server on UDP %d, background sink UDP %d" % (self.port, self.bg_port))
        next_house = next_write = time.time()
        while self.running:
            try:
                data, addr = self.sock.recvfrom(4096)
                self.handle(data, addr)
            except socket.timeout:
                pass
            except OSError:
                pass
            now = time.time()
            if now >= next_house:
                self.housekeeping()
                next_house = now + HOUSEKEEP_EVERY
            if now >= next_write:
                self.write_state()
                next_write = now + WRITE_EVERY
        self.log("server stopped")


def main():
    parser = argparse.ArgumentParser(description="UDP game server")
    parser.add_argument("--port", type=int, default=GAME_PORT)
    parser.add_argument("--bg-port", type=int, default=BG_PORT)
    parser.add_argument("--state-file", default="/tmp/cn_game_state.json")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    server = GameServer(args.port, args.bg_port, args.state_file, args.quiet)
    stop = lambda *_: setattr(server, "running", False)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    server.run()


if __name__ == "__main__":
    main()
