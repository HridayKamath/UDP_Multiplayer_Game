"""
selftest.py - live test cases, run from the "Tests" tab of the web UI

Each test checks one requirement on the REAL running lab and records PASS / FAIL / SKIP with a
short detail line. The results are saved to results/selftest.json and appear in the report.
Total run time: about 90 seconds.
"""
import json
import os
import threading
import time

import topology

HERE = os.path.dirname(os.path.abspath(__file__))


class Skip(Exception):
    pass


class SelfTestRunner:
    def __init__(self, manager):
        self.m = manager
        self.abort = False
        self.status = {"running": False, "results": [], "current": "", "time": ""}

    def start(self):
        m = self.m
        if not m.lab.running:
            return {"ok": False, "msg": "Start the network first."}
        self.abort = False
        self.status = {"running": True, "results": [], "current": "", "time": time.strftime("%Y-%m-%d %H:%M:%S")}
        m.busy = "selftest"
        threading.Thread(target=self._run, daemon=True).start()
        return {"ok": True, "msg": "self-tests started (about 90 s)"}

    def stop(self):
        self.abort = True
        return {"ok": True, "msg": "stopping after the current test"}

    # ------------------------------------------------------------ helpers
    def wait(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            if self.abort:
                raise KeyboardInterrupt
            time.sleep(0.25)

    def wait_until(self, cond, timeout):
        end = time.time() + timeout
        while time.time() < end:
            if self.abort:
                raise KeyboardInterrupt
            self.m.refresh()
            if cond():
                return True
            time.sleep(0.5)
        return False

    def players(self):
        return {p["id"]: p for p in self.m.cache.get("game", {}).get("players", [])}

    def sdn_only(self):
        if self.m.lab.name == "local":
            raise Skip("needs the Mininet/Ryu lab (not available in local dev mode)")

    # ------------------------------------------------------------ the tests
    def t_controller(self):
        self.sdn_only()
        c = self.m.cache.get("controller", {})
        assert c.get("ok") and c.get("switch_connected"), "controller/switch not connected: %s" % c
        return "Ryu answers on UDP 9998 and OVS switch s1 is connected (OpenFlow 1.3)"

    def t_connectivity(self):
        self.sdn_only()
        bad = [h for h in ("client1", "client2", "client3", "client4", "bg") if not self.m.lab.ping(h)]
        assert not bad, "no connectivity to server from: " + ", ".join(bad)
        return "client1-4 and bg can ping the server through the SDN switch"

    def t_flows(self):
        self.sdn_only()
        flows, _raw = self.m.lab.flows()
        prios = {f["priority"] for f in flows}
        missing = [p for p in (200, 100, 0) if p not in prios]
        assert not missing, "missing flow priorities %s" % missing
        game = [f for f in flows if f["priority"] == 200 and "9999" in f["match"] and "output:5" in f["actions"]]
        assert game, "no game rule towards the server port"
        return "installed: game rule (prio 200) %s, background (100), table-miss (0)" % game[0]["actions"]

    def t_qos(self):
        self.sdn_only()
        assert self.m.lab.qos_attached(), "no QoS object attached to %s" % topology.BOTTLENECK_IF
        stats, raw = self.m.lab.queue_stats()
        assert 0 in stats and 1 in stats, "queue-stats does not list queues 0 and 1: %r" % raw[:200]
        return "HTB QoS attached to %s with queue 0 (best effort) and queue 1 (game)" % topology.BOTTLENECK_IF

    def t_register(self):
        m = self.m
        m.stop_everything()
        m.start_server()
        for i, host in enumerate(("client1", "client2", "client3")):
            m.add_player("t%d" % (i + 1), host)
        ok = self.wait_until(lambda: len([p for p in self.players().values() if p["status"] == "connected"]) >= 3, 12)
        pl = self.players()
        assert ok, "players connected: %s" % {k: v["status"] for k, v in pl.items()}
        self.wait(2)
        pl = self.players()
        assert all(p["seq"] > 5 for p in pl.values()), "sequence numbers not advancing"
        return "3 players registered from 3 hosts; server tracks sequence numbers (%s)" % \
            ", ".join("%s seq=%d" % (k, v["seq"]) for k, v in sorted(pl.items()))

    def t_latency_jitter(self):
        pl = self.players()
        assert pl and all(p["rtt_ms"] is not None and p["jitter_ms"] is not None for p in pl.values()), \
            "RTT/jitter missing: %s" % {k: (v["rtt_ms"], v["jitter_ms"]) for k, v in pl.items()}
        return "RTT and jitter measured: " + ", ".join("%s %.1f ms / %.2f ms" % (k, v["rtt_ms"], v["jitter_ms"])
                                                       for k, v in sorted(pl.items()))

    def t_loss(self):
        self.sdn_only()
        m = self.m
        m.set_conditions(0, 0, 10)
        try:
            self.wait(9)
            self.m.refresh()
            pl = self.players().values()
            up = sum(p["up_lost"] for p in pl)
            down = sum(p["down_lost"] for p in pl)
            assert up > 0 or down > 0, "no loss detected under 10 % configured loss"
        finally:
            m.set_conditions(0, 0, 0)
        return "with 10 %% loss configured: %d updates detected missing upstream, %d reported missing downstream" % (up, down)

    def t_delay(self):
        self.sdn_only()
        m = self.m
        m.set_conditions(50, 0, 0)
        try:
            self.wait(7)
            self.m.refresh()
            rtts = [p["rtt_ms"] for p in self.players().values() if p["rtt_ms"] is not None]
            assert rtts and sum(rtts) / len(rtts) > 80, "RTT did not reflect 50 ms each way: %s" % rtts
        finally:
            m.set_conditions(0, 0, 0)
        return "with 50 ms delay each way the measured RTT rose to %.0f ms" % (sum(rtts) / len(rtts))

    def t_block(self):
        self.sdn_only()
        m = self.m
        ip = topology.HOST_IPS["client1"]
        r = m.policy({"cmd": "block", "ip": ip})
        assert r.get("ok"), r
        try:
            self.wait(1.5)
            flows, _ = m.lab.flows()
            drops = [f for f in flows if f["priority"] == 300 and "drop" in f["actions"]]
            assert len(drops) >= 2, "priority-300 drop rules not installed"
            ok = self.wait_until(lambda: self.players().get("t1", {}).get("status") == "disconnected", 8)
            assert ok, "blocked player t1 still connected"
            assert self.players()["t2"]["status"] == "connected", "other players were affected by the block"
        finally:
            m.policy({"cmd": "unblock", "ip": ip})
        self.wait(1.5)
        flows, _ = m.lab.flows()
        assert not [f for f in flows if f["priority"] == 300], "drop rules not removed"
        ok = self.wait_until(lambda: self.players().get("t1", {}).get("status") == "connected", 10)
        assert ok, "player did not reconnect after unblock"
        return "block installed 2 priority-300 drop rules, t1 disconnected while t2/t3 stayed up; unblock removed them and t1 reconnected"

    def t_disconnect(self):
        m = self.m
        m.remove_player("t3")
        ok = self.wait_until(lambda: self.players().get("t3", {}).get("status") == "disconnected", 8)
        assert ok, "stopped player is still shown connected"
        m.add_player("t3", "client3")
        ok = self.wait_until(lambda: self.players().get("t3", {}).get("status") == "connected", 8)
        assert ok, "player could not re-register"
        return "a stopped player is marked disconnected and can register again"

    def t_qos_queues(self):
        self.sdn_only()
        m = self.m
        m.policy({"cmd": "qos", "mode": "on"})
        before, _ = m.lab.queue_stats()
        m.start_bg(14, 6)
        self.wait(8)
        after, _ = m.lab.queue_stats()
        q0 = (after.get(0, {}).get("bytes", 0) - before.get(0, {}).get("bytes", 0)) / 1e6
        q1 = (after.get(1, {}).get("bytes", 0) - before.get(1, {}).get("bytes", 0)) / 1e6
        assert q0 > 1.0 and q1 > 0.0, "queue counters did not separate game and background (q0 %.2f MB, q1 %.3f MB)" % (q0, q1)
        return "background went through queue 0 (%.1f MB), game traffic through queue 1 (%.2f MB)" % (q0, q1)

    def t_guard(self):
        self.sdn_only()
        m = self.m
        m.stop_bg()
        m.guard.update(enabled=True, max_pps=60, strikes={}, auto_blocked=[])
        ip = topology.HOST_IPS["client4"]
        try:
            m.flood()
            ok = self.wait_until(lambda: ip in m.cache.get("blocked", []), 12)
            assert ok, "flooding client was not blocked automatically"
        finally:
            m.guard["enabled"] = False
            m.guard["auto_blocked"] = []
            m.stop_proc(m.aux.pop("flood", None))
            m.policy({"cmd": "unblock", "ip": ip})
        return "a client flooding 200 updates/s was detected by the server and blocked by the controller automatically"

    TESTS = [
        ("Controller and switch connected", "Mininet + SDN", "t_controller"),
        ("Host connectivity through the switch", "Mininet + SDN", "t_connectivity"),
        ("OpenFlow rules installed (game / background / table-miss)", "SDN flow rules", "t_flows"),
        ("QoS queues exist and are attached to the bottleneck port", "Traffic prioritisation", "t_qos"),
        ("Multiple players register, sequence numbers tracked", "Application: register players", "t_register"),
        ("RTT and jitter are measured", "Application: latency, jitter", "t_latency_jitter"),
        ("Packet loss is detected under simulated loss", "Application: packet loss", "t_loss"),
        ("Added delay is visible in the measured RTT", "Application: latency", "t_delay"),
        ("Dynamic block / unblock of a client", "Dynamic SDN", "t_block"),
        ("Disconnect handling and re-registration", "Robustness", "t_disconnect"),
        ("Game and background traffic use different queues", "Traffic prioritisation", "t_qos_queues"),
        ("Application event triggers SDN reaction (auto-guard)", "Socket-SDN integration", "t_guard"),
    ]

    def _run(self):
        m = self.m
        try:
            m.log("Self-tests started")
            for name, req, fn in self.TESTS:
                if self.abort:
                    break
                self.status["current"] = name
                try:
                    detail = getattr(self, fn)()
                    status = "pass"
                except Skip as e:
                    status, detail = "skip", str(e)
                except AssertionError as e:
                    status, detail = "fail", str(e)
                except KeyboardInterrupt:
                    break
                except Exception as e:
                    status, detail = "fail", "error: %s" % e
                self.status["results"].append({"name": name, "req": req, "status": status, "detail": detail})
        finally:
            try:
                m.stop_everything()
                m.set_conditions(0, 0, 0)
                m.policy({"cmd": "qos", "mode": "on"})
            except Exception:
                pass
            res = self.status["results"]
            self.status.update(running=False, current="")
            with open(os.path.join(HERE, "results", "selftest.json"), "w") as f:
                json.dump({"time": self.status["time"], "results": res}, f, indent=1)
            passed = sum(1 for r in res if r["status"] == "pass")
            m.log("Self-tests finished: %d/%d passed" % (passed, len(res)), "ok" if passed == len(res) else "warn")
            m.busy = ""
