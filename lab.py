"""
lab.py - the "network under test": Mininet backend + a local backend for development

The web manager only talks to a Lab object, so the same UI works with
  * MininetLab : the real thing (Mininet + OVS + Ryu), needs root
  * LocalLab   : plain local processes on 127.0.0.1 (no SDN) - only for developing the UI

Also contains the parsers for `ovs-ofctl dump-flows` / `queue-stats` and the netem
command builder (pure functions, covered by tests/).
"""
import os
import re
import subprocess
import sys
import time

import topology

HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- pure helpers
def netem_cmd(dev, delay, jitter, loss):
    """tc command that applies delay/jitter/loss on one interface (or removes netem)."""
    if not (delay or jitter or loss):
        return "tc qdisc del dev %s root 2>/dev/null" % dev
    cmd = "tc qdisc replace dev %s root netem" % dev
    if delay or jitter:
        cmd += " delay %gms" % delay
        if jitter:
            cmd += " %gms" % jitter
    if loss:
        cmd += " loss %g%%" % loss
    return cmd


FLOW_RE = re.compile(r"n_packets=(\d+), n_bytes=(\d+),(.*?)priority=(\d+)(.*?)\s+actions=(.*)$")
FLOW_LABELS = {300: "BLOCK (drop)", 200: "GAME", 100: "BACKGROUND", 10: "L2 learned", 0: "TABLE MISS"}


def parse_flows(text):
    """`ovs-ofctl dump-flows` text -> list of dicts (highest priority first)."""
    flows = []
    for line in text.splitlines():
        m = FLOW_RE.search(line)
        if not m:
            continue
        priority = int(m.group(4))
        flows.append({
            "priority": priority,
            "label": FLOW_LABELS.get(priority, "other"),
            "match": m.group(5).strip(", ") or "(any)",
            "actions": m.group(6).strip(),
            "packets": int(m.group(1)),
            "bytes": int(m.group(2)),
        })
    flows.sort(key=lambda f: (-f["priority"], f["match"]))
    return flows


QUEUE_RE = re.compile(r"queue\s+(\d+)\s*:(.*)")


def parse_queue_stats(text):
    """`ovs-ofctl queue-stats` text -> {queue_id: {"bytes":..,"packets":..,"drops":..}}."""
    result = {}
    for line in text.splitlines():
        m = QUEUE_RE.search(line)
        if not m:
            continue
        rest = m.group(2)
        b = re.search(r"(?:tx_)?bytes[=:]\s*(\d+)", rest)
        p = re.search(r"(?:tx_)?pkts[=:]\s*(\d+)|packets[=:]\s*(\d+)", rest)
        d = re.search(r"(?:pkts )?(?:dropped|drops|tx_errors)[=:]\s*(\d+)", rest)
        pk = 0
        if p:
            pk = int(p.group(1) or p.group(2))
        result[int(m.group(1))] = {"bytes": int(b.group(1)) if b else 0, "packets": pk,
                                   "drops": int(d.group(1)) if d else 0}
    return result


# ---------------------------------------------------------------- backends
class Lab:
    name = "base"
    server_ip = topology.HOST_IPS["server"]
    running = False

    def start(self, bw):                    # -> {"ok":bool, "msg":str}
        raise NotImplementedError

    def stop(self):
        pass

    def popen(self, host, argv, logfile):
        raise NotImplementedError

    def set_impairment(self, delay, jitter, loss):
        pass

    def set_bandwidth(self, mbit):
        return True, "not supported"

    def flows(self):                        # -> (list, raw_text)
        return [], ""

    def queue_stats(self):                  # -> (dict, raw_text)
        return {}, ""

    def qos_attached(self):
        return False

    def ping(self, host):                   # -> bool
        return True


class LocalLab(Lab):
    """Development backend: everything runs on this machine, no SDN, no impairments."""
    name = "local"
    server_ip = "127.0.0.1"

    def start(self, bw):
        self.running = True
        return {"ok": True, "msg": "LOCAL DEV MODE: no Mininet / OVS / Ryu (SDN features unavailable)"}

    def stop(self):
        self.running = False

    def popen(self, host, argv, logfile):
        f = open(logfile, "w")
        return subprocess.Popen([sys.executable, "-u"] + argv, stdout=f, stderr=subprocess.STDOUT, cwd=HERE)


class MininetLab(Lab):
    name = "mininet"

    def __init__(self):
        self.net = None
        self.bw = 10.0

    def start(self, bw):
        self.bw = bw
        self.net = topology.build_network()
        self.net.start()
        connected = True
        try:
            self.net.waitConnected(timeout=15)
        except Exception:
            connected = False
        qos_ok, qos_out = topology.setup_qos(bw)
        self.running = True
        if connected:                         # warm up ARP so L2 rules are learned
            for h in ("client1", "client2", "client3", "client4", "bg"):
                self.ping(h)
        msg = []
        if not connected:
            msg.append("switch did NOT connect to Ryu")
        if not qos_ok:
            msg.append("QoS setup failed: " + qos_out[:200])
        return {"ok": connected and qos_ok, "msg": "; ".join(msg) or "network up, QoS queues attached"}

    def stop(self):
        if self.net is not None:
            self.net.stop()
            self.net = None
        self.running = False

    def popen(self, host, argv, logfile):
        f = open(logfile, "w")
        return self.net.get(host).popen([sys.executable, "-u"] + argv, stdout=f,
                                        stderr=subprocess.STDOUT, cwd=HERE)

    def set_impairment(self, delay, jitter, loss):
        """netem on BOTH directions of every client link (client egress + switch egress)."""
        for i in range(1, 5):
            host = self.net.get("client%d" % i)
            host.cmd(netem_cmd("client%d-eth0" % i, delay, jitter, loss))
            topology.run(netem_cmd("s1-eth%d" % i, delay, jitter, loss))

    def set_bandwidth(self, mbit):
        self.bw = mbit
        ok, out = topology.setup_qos(mbit)
        return ok, out

    def flows(self):
        _ok, out = topology.run("ovs-ofctl -O OpenFlow13 dump-flows s1")
        return parse_flows(out), out

    def queue_stats(self):
        _ok, out = topology.run("ovs-ofctl -O OpenFlow13 queue-stats s1 5")
        return parse_queue_stats(out), out

    def qos_attached(self):
        ok, out = topology.run("ovs-vsctl get port %s qos" % topology.BOTTLENECK_IF)
        return ok and out.strip() not in ("", "[]")

    def ping(self, host):
        out = self.net.get(host).cmd("ping -c 1 -W 1 %s" % topology.HOST_IPS["server"])
        return " 0% packet loss" in out or "1 received" in out
