#!/usr/bin/env python3
"""
manager.py - web manager (Flask) for the UDP game over SDN lab

Run with  ./start.sh  (needs root for Mininet).  Browser UI: http://127.0.0.1:8000

The manager
  * starts Ryu and the Mininet network (via lab.py), attaches the OVS QoS queues
  * starts/stops the game server and any number of players (processes inside Mininet hosts)
  * applies network conditions (delay, jitter, loss, bottleneck bandwidth)
  * sends policy commands to the controller (block / unblock / QoS on-off) and shows the replies
  * polls the OpenFlow table, queue counters and game state once per second for the UI
  * runs the automatic experiments (experiments.py) and the self-tests (selftest.py)
Environment: LAB=local runs a development mode without Mininet/Ryu.
"""
import atexit
import csv
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque

from flask import Flask, jsonify, request, render_template, send_file, Response

import topology
from lab import LocalLab, MininetLab
from policy import send_policy
import experiments
import selftest

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.environ.get("CN_STATE_FILE", "/tmp/cn_d2_state.json")
RESULTS_DIR = os.path.join(BASE, "results")
LOGS_DIR = os.path.join(BASE, "logs")
MAX_PLAYERS = 12


def path(*parts):
    return os.path.join(BASE, *parts)


class Manager:
    def __init__(self, lab):
        self.lab = lab
        self.lock = threading.RLock()
        self.ryu_proc = None
        self.server_proc = None
        self.players = {}                 # player id -> {"host": str, "proc": Popen}
        self.aux = {}                     # "bg" / "flood" -> Popen
        self.bg_info = {"running": False, "mbps": 0, "until": 0}
        self.cond = {"delay": 0.0, "jitter": 0.0, "loss": 0.0, "bw": 10.0}
        self.qos = "on"
        self.events = deque(maxlen=300)
        self.cache = {}
        self.prev_queue = None
        self.prev_status = {"players": {}, "switch": None, "blocked": None}
        self.guard = {"enabled": False, "max_pps": 60, "strikes": {}, "auto_blocked": []}
        self.busy = ""                    # "experiment" / "selftest" while they own the network
        self.experiments = experiments.ExperimentRunner(self)
        self.selftest = selftest.SelfTestRunner(self)
        os.makedirs(RESULTS_DIR, exist_ok=True)
        os.makedirs(LOGS_DIR, exist_ok=True)

    # ------------------------------------------------------------ logging
    def log(self, text, kind="info"):
        self.events.appendleft({"t": time.strftime("%H:%M:%S"), "kind": kind, "msg": text})

    # ------------------------------------------------------------ processes
    def spawn(self, host, argv, logname):
        """Start a python script inside a Mininet host (or locally in dev mode)."""
        argv = [path(argv[0])] + list(argv[1:])
        return self.lab.popen(host, argv, path("logs", logname))

    @staticmethod
    def stop_proc(proc, wait=2.0):
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=wait)
            except subprocess.TimeoutExpired:
                proc.kill()

    def start_ryu(self):
        if self.lab.name == "local":
            return
        if self.ryu_proc and self.ryu_proc.poll() is None:
            return
        exe = os.environ.get("RYU_MANAGER", "")
        if not exe:
            for cand in (os.path.expanduser("~/cn-project/ryu-env/bin/ryu-manager"),
                         "/usr/local/bin/ryu-manager", "/usr/bin/ryu-manager"):
                if os.path.isfile(cand):
                    exe = cand
                    break
        if not exe:
            raise RuntimeError("ryu-manager not found. Set RYU_MANAGER=/path/to/ryu-manager")
        log = open(path("logs", "ryu.log"), "w")
        self.ryu_proc = subprocess.Popen(
            [exe, "--ofp-tcp-listen-port", str(topology.RYU_PORT), path("controller.py")],
            cwd=BASE, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.ryu_proc.poll() is not None:
                raise RuntimeError("Ryu exited - see logs/ryu.log")
            s = socket.socket()
            try:
                s.connect(("127.0.0.1", topology.RYU_PORT))
                s.close()
                break
            except OSError:
                s.close()
                time.sleep(0.3)
        else:
            raise RuntimeError("Ryu did not open port %d in time (logs/ryu.log)" % topology.RYU_PORT)
        for _ in range(20):                  # wait for the policy listener
            if send_policy({"cmd": "status"}, timeout=0.5).get("ok"):
                return
            time.sleep(0.3)
        raise RuntimeError("Ryu policy listener (UDP 9998) not answering - see logs/ryu.log")

    # ------------------------------------------------------------ network lifecycle
    def start_network(self):
        with self.lock:
            if self.lab.running:
                return {"ok": True, "msg": "network already running"}
            self.start_ryu()
            self.log("Ryu controller started" if self.lab.name != "local" else "local dev mode")
            res = self.lab.start(self.cond["bw"])
            self.qos = "on"
            if self.lab.name != "local":
                send_policy({"cmd": "qos", "mode": "on"})
            self.log("Network: " + res["msg"], "ok" if res["ok"] else "error")
            return res

    def stop_network(self):
        with self.lock:
            self.stop_everything()
            self.lab.stop()
            self.stop_proc(self.ryu_proc, 4)
            self.ryu_proc = None
            self.cond.update(delay=0.0, jitter=0.0, loss=0.0)
            self.log("Network stopped")
        return {"ok": True, "msg": "network stopped"}

    def need_network(self):
        if not self.lab.running:
            raise RuntimeError("Start the network first.")

    # ------------------------------------------------------------ game
    def start_server(self):
        with self.lock:
            self.need_network()
            if self.server_proc and self.server_proc.poll() is None:
                return {"ok": True, "msg": "server already running"}
            try:
                os.unlink(STATE_FILE)
            except OSError:
                pass
            self.server_proc = self.spawn("server", ["server.py", "--state-file", STATE_FILE], "server.log")
            time.sleep(0.6)
            if self.server_proc.poll() is not None:
                raise RuntimeError("game server failed - see logs/server.log")
            self.log("Game server started (UDP 9999)")
        return {"ok": True, "msg": "game server started"}

    def stop_server(self):
        with self.lock:
            self.stop_proc(self.server_proc)
            self.server_proc = None
        return {"ok": True, "msg": "game server stopped"}

    def next_player_id(self):
        for i in range(1, 100):
            if "p%d" % i not in self.players or self.players["p%d" % i]["proc"].poll() is not None:
                return "p%d" % i
        raise RuntimeError("too many players")

    def pick_host(self):
        load = {h: 0 for h in topology.CLIENT_HOSTS}
        for p in self.players.values():
            if p["proc"].poll() is None and p["host"] in load:
                load[p["host"]] += 1
        return min(topology.CLIENT_HOSTS, key=lambda h: (load[h], h))

    def add_player(self, pid=None, host=None):
        """Register a new player: starts a client process (any id, any client host)."""
        with self.lock:
            self.need_network()
            if not (self.server_proc and self.server_proc.poll() is None):
                self.start_server()
            pid = (pid or "").strip() or self.next_player_id()
            from common import clean_id
            if clean_id(pid) is None:
                raise ValueError("player id: 1-16 letters, digits, _ or -")
            old = self.players.get(pid)
            if old and old["proc"].poll() is None:
                raise ValueError("player %s is already running" % pid)
            alive = [p for p in self.players.values() if p["proc"].poll() is None]
            if len(alive) >= MAX_PLAYERS:
                raise ValueError("maximum %d players" % MAX_PLAYERS)
            host = host if host in topology.CLIENT_HOSTS else self.pick_host()
            proc = self.spawn(host, ["client.py", pid, "--server", self.lab.server_ip], pid + ".log")
            self.players[pid] = {"host": host, "proc": proc}
            self.log("Player %s registered from %s (%s)" % (pid, host, topology.HOST_IPS[host]), "ok")
        return {"ok": True, "msg": "player %s started on %s" % (pid, host)}

    def remove_player(self, pid):
        with self.lock:
            p = self.players.pop(pid, None)
            if p is None:
                raise ValueError("player %s is not running" % pid)
            self.stop_proc(p["proc"])
            self.log("Player %s left" % pid)
        return {"ok": True, "msg": "player %s stopped" % pid}

    def stop_players(self):
        for pid in list(self.players):
            try:
                self.remove_player(pid)
            except ValueError:
                pass

    def move(self, pid, dx, dy):
        tmp = "/tmp/cn_ctl_%s.json.tmp" % pid
        with open(tmp, "w") as f:
            json.dump({"dx": dx, "dy": dy}, f)
        os.replace(tmp, "/tmp/cn_ctl_%s.json" % pid)
        return {"ok": True, "msg": "moved"}

    def stop_everything(self):
        self.stop_players()
        self.stop_proc(self.aux.pop("bg", None))
        self.stop_proc(self.aux.pop("flood", None))
        self.bg_info["running"] = False
        self.stop_server()

    # ------------------------------------------------------------ conditions
    def set_conditions(self, delay=0, jitter=0, loss=0, bw=None):
        self.need_network()
        delay = max(0.0, min(500.0, float(delay)))
        jitter = max(0.0, min(200.0, float(jitter)))
        loss = max(0.0, min(50.0, float(loss)))
        with self.lock:
            self.lab.set_impairment(delay, jitter, loss)
            self.cond.update(delay=delay, jitter=jitter, loss=loss)
            if bw is not None and float(bw) != self.cond["bw"]:
                bw = max(1.0, min(100.0, float(bw)))
                ok, out = self.lab.set_bandwidth(bw)
                self.cond["bw"] = bw
                if not ok:
                    raise RuntimeError("could not change bandwidth: " + out[:200])
            self.log("Conditions: delay %gms, jitter %gms, loss %g%%, bottleneck %g Mbit/s"
                     % (self.cond["delay"], self.cond["jitter"], self.cond["loss"], self.cond["bw"]))
        return {"ok": True, "msg": "conditions applied"}

    def start_bg(self, mbps=14.0, secs=30.0):
        with self.lock:
            self.need_network()
            self.stop_proc(self.aux.pop("bg", None))
            mbps = max(0.5, min(100.0, float(mbps)))
            secs = max(1.0, min(600.0, float(secs)))
            self.aux["bg"] = self.spawn("bg", ["traffic_generator.py", "--dst", self.lab.server_ip,
                                               "--mbps", str(mbps), "--duration", str(secs)], "bg.log")
            self.bg_info = {"running": True, "mbps": mbps, "until": time.time() + secs}
            self.log("Background traffic: %g Mbit/s for %g s -> server UDP 10000" % (mbps, secs), "warn")
        return {"ok": True, "msg": "background traffic started"}

    def stop_bg(self):
        with self.lock:
            self.stop_proc(self.aux.pop("bg", None))
            self.bg_info["running"] = False
            self.log("Background traffic stopped")
        return {"ok": True, "msg": "background traffic stopped"}

    def flood(self, host="client4"):
        """A misbehaving client: 200 updates/s for 12 s (for the auto-guard demo)."""
        with self.lock:
            self.need_network()
            if not (self.server_proc and self.server_proc.poll() is None):
                self.start_server()
            self.stop_proc(self.aux.pop("flood", None))
            self.aux["flood"] = self.spawn(host, ["client.py", "flood", "--server", self.lab.server_ip,
                                                  "--rate", "200", "--duration", "12", "--quiet"], "flood.log")
            self.log("Flooding client 'flood' started on %s (200 updates/s)" % host, "warn")
        return {"ok": True, "msg": "flood started"}

    # ------------------------------------------------------------ SDN policy
    def policy(self, req):
        if self.lab.name == "local":
            return {"ok": False, "msg": "no SDN controller in local dev mode"}
        reply = send_policy(req)
        kind = "ok" if reply.get("ok") else "error"
        if req.get("cmd") in ("block", "unblock", "qos"):
            self.log("SDN %s %s -> %s" % (req["cmd"], req.get("ip") or req.get("mode") or "",
                                          reply.get("msg")), kind)
        if req.get("cmd") == "qos" and reply.get("ok"):
            self.qos = req["mode"]
        return reply

    def player_ip(self, pid):
        for p in self.cache.get("game", {}).get("players", []):
            if p["id"] == pid:
                return p["ip"]
        if pid in self.players:
            return topology.HOST_IPS[self.players[pid]["host"]]
        raise ValueError("unknown player %s" % pid)

    # ------------------------------------------------------------ polling
    def read_state(self):
        try:
            with open(STATE_FILE) as f:
                state = json.load(f)
        except (OSError, ValueError):
            return {"players": [], "bg": None, "server_up": False}
        state["server_up"] = (time.time() - state.get("server_time", 0)) < 3.0
        if not state["server_up"]:
            for p in state.get("players", []):
                p["status"] = "disconnected"
        return state

    def refresh(self):
        game = self.read_state()
        ctl = {"ok": False}
        flows, queues, raw_q = [], {}, ""
        if self.lab.name != "local":
            ctl = send_policy({"cmd": "status"}, timeout=0.4)
            if self.lab.running:
                flows, _raw = self.lab.flows()
                queues, raw_q = self.lab.queue_stats()
        now = time.time()
        rates = {}
        if self.prev_queue and queues:
            dt = now - self.prev_queue[0]
            for qid, v in queues.items():
                old = self.prev_queue[1].get(qid, {}).get("bytes", v["bytes"])
                rates[qid] = round(max(0, v["bytes"] - old) * 8 / dt / 1e6, 2) if dt > 0 else 0
        if queues:
            self.prev_queue = (now, queues)
        for qid, v in queues.items():
            v["mbps"] = rates.get(qid, 0)
        if self.bg_info["running"] and now > self.bg_info["until"] + 1:
            self.bg_info["running"] = False
        self.cache = {
            "time": now, "lab": self.lab.name, "network": self.lab.running, "busy": self.busy,
            "server_running": bool(self.server_proc and self.server_proc.poll() is None),
            "game": game, "controller": ctl, "flows": flows, "queues": queues,
            "queue_raw": raw_q, "conditions": self.cond, "qos": ctl.get("qos", self.qos) if ctl.get("ok") else self.qos,
            "blocked": ctl.get("blocked", []) if ctl.get("ok") else [],
            "processes": {pid: {"host": p["host"], "alive": p["proc"].poll() is None}
                          for pid, p in self.players.items()},
            "bg": dict(self.bg_info), "guard": {k: self.guard[k] for k in ("enabled", "max_pps", "auto_blocked")},
            "events": list(self.events)[:120],
            "experiment": self.experiments.status, "selftest": self.selftest.status,
            "hosts": topology.HOST_IPS,
        }
        self.detect_events(game, ctl)
        self.run_guard(game)

    def detect_events(self, game, ctl):
        prev = self.prev_status
        for p in game.get("players", []):
            old = prev["players"].get(p["id"])
            if old != p["status"] and old is not None:
                self.log("Player %s is now %s" % (p["id"], p["status"].upper()),
                         "ok" if p["status"] == "connected" else "warn")
            prev["players"][p["id"]] = p["status"]
        if ctl.get("ok"):
            if prev["switch"] is not None and prev["switch"] != ctl["switch_connected"]:
                self.log("Switch %s" % ("connected" if ctl["switch_connected"] else "DISCONNECTED"),
                         "ok" if ctl["switch_connected"] else "error")
            prev["switch"] = ctl["switch_connected"]

    def run_guard(self, game):
        """Application event -> SDN reaction: a client sending far too many updates gets blocked."""
        g = self.guard
        if not g["enabled"] or not self.cache.get("controller", {}).get("ok"):
            return
        for p in game.get("players", []):
            ip = p["ip"]
            if p["status"] != "connected" or ip in self.cache.get("blocked", []):
                g["strikes"][ip] = 0
                continue
            if p["pps"] > g["max_pps"]:
                g["strikes"][ip] = g["strikes"].get(ip, 0) + 1
                if g["strikes"][ip] >= 2:
                    reply = send_policy({"cmd": "block", "ip": ip})
                    self.log("AUTO-GUARD: %s (%s) sends %d updates/s (limit %d) -> BLOCKED: %s"
                             % (p["id"], ip, p["pps"], g["max_pps"], reply.get("msg")), "warn")
                    if reply.get("ok") and ip not in g["auto_blocked"]:
                        g["auto_blocked"].append(ip)
                    g["strikes"][ip] = 0
            else:
                g["strikes"][ip] = 0

    def poll_loop(self):
        while True:
            try:
                self.refresh()
            except Exception as e:                     # never let the poller die
                self.log("poller error: %s" % e, "error")
            time.sleep(1.0)

    # ------------------------------------------------------------ actions (from the UI)
    def action(self, req):
        name = req.get("action")
        if self.busy and name not in ("experiment_stop", "selftest_stop"):
            return {"ok": False, "msg": "busy: %s is running - wait or stop it first" % self.busy}
        try:
            if name == "network_start":
                return self.start_network()
            if name == "network_stop":
                return self.stop_network()
            if name == "demo_start":
                res = self.start_network()
                if not res.get("ok") and not self.lab.running:
                    return res
                self.start_server()
                for _ in range(4):
                    self.add_player()
                return {"ok": res.get("ok", True), "msg": "demo running: network, server, 4 players. " + res["msg"]}
            if name == "game_stop":
                self.stop_everything()
                return {"ok": True, "msg": "game stopped"}
            if name == "server_start":
                return self.start_server()
            if name == "player_add":
                return self.add_player(req.get("player"), req.get("host"))
            if name == "player_remove":
                return self.remove_player(req.get("player"))
            if name == "player_move":
                return self.move(req["player"], req.get("dx", 0), req.get("dy", 0))
            if name in ("block", "unblock"):
                ip = req.get("ip") or self.player_ip(req.get("player"))
                return self.policy({"cmd": name, "ip": ip})
            if name == "qos":
                return self.policy({"cmd": "qos", "mode": req.get("mode")})
            if name == "conditions":
                return self.set_conditions(req.get("delay", 0), req.get("jitter", 0),
                                           req.get("loss", 0), req.get("bw"))
            if name == "bg_start":
                return self.start_bg(req.get("mbps", 14), req.get("secs", 30))
            if name == "bg_stop":
                return self.stop_bg()
            if name == "flood":
                return self.flood()
            if name == "guard":
                self.guard["enabled"] = bool(req.get("enabled"))
                self.guard["max_pps"] = int(req.get("max_pps", 60))
                if not self.guard["enabled"]:
                    self.guard["auto_blocked"] = []
                self.log("Auto-guard %s (limit %d updates/s per player)"
                         % ("ENABLED" if self.guard["enabled"] else "disabled", self.guard["max_pps"]))
                return {"ok": True, "msg": "auto-guard updated"}
            if name == "experiment_start":
                return self.experiments.start(req.get("scenarios"), int(req.get("duration", 20)),
                                              int(req.get("repeats", 1)))
            if name == "experiment_stop":
                return self.experiments.stop()
            if name == "selftest_start":
                return self.selftest.start()
            if name == "selftest_stop":
                return self.selftest.stop()
            return {"ok": False, "msg": "unknown action %r" % name}
        except Exception as e:
            self.log("%s failed: %s" % (name, e), "error")
            return {"ok": False, "msg": str(e)}


# ============================================================ Flask app
app = Flask(__name__)
manager = None


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/state")
def api_state():
    return jsonify(manager.cache or {"booting": True})


@app.route("/api/action", methods=["POST"])
def api_action():
    req = request.get_json(silent=True) or {}
    res = manager.action(req)
    manager.refresh()
    return jsonify(res), (200 if res.get("ok") else 400)


@app.route("/api/results")
def api_results():
    rows = experiments.load_rows(RESULTS_DIR)
    return jsonify({"rows": rows, "summary": experiments.summarize(rows),
                    "charts": experiments.make_charts(experiments.summarize(rows)),
                    "findings": experiments.findings(experiments.summarize(rows))})


@app.route("/report")
def report():
    return Response(experiments.build_report(RESULTS_DIR), mimetype="text/html")


@app.route("/download/results.csv")
def download_csv():
    p = os.path.join(RESULTS_DIR, "results.csv")
    if not os.path.isfile(p):
        return jsonify(error="no results yet"), 404
    return send_file(p, as_attachment=True)


@app.route("/api/logs/<name>")
def api_logs(name):
    if name not in ("server", "ryu", "bg") and not (name.startswith("p") and name[1:].isdigit()):
        return jsonify(error="unknown log"), 404
    p = path("logs", name + ".log")
    if not os.path.isfile(p):
        return jsonify(lines=[])
    with open(p, errors="replace") as f:
        return jsonify(lines=f.read().splitlines()[-40:])


@app.route("/api/experiment/reset", methods=["POST"])
def api_reset_results():
    p = os.path.join(RESULTS_DIR, "results.csv")
    if os.path.isfile(p):
        os.rename(p, p + ".%d.bak" % int(time.time()))
    return jsonify(ok=True)


def shutdown(*_):
    try:
        manager.busy = ""
        manager.stop_network()
    finally:
        os._exit(0)


def main():
    global manager
    if os.environ.get("LAB") == "local":
        lab = LocalLab()
    else:
        if os.geteuid() != 0:
            sys.exit("Run with sudo (Mininet needs root): use ./start.sh")
        lab = MininetLab()
    manager = Manager(lab)
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    atexit.register(lambda: manager.lab.running and manager.stop_network())
    threading.Thread(target=manager.poll_loop, daemon=True).start()
    host = os.environ.get("CN_HOST", "127.0.0.1")
    port = int(os.environ.get("CN_PORT", "8000"))
    print("UDP game SDN lab - open http://%s:%d  (lab backend: %s)" % (host, port, lab.name), flush=True)
    app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
