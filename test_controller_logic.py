"""Controller logic tested with a stub of the Ryu API (Ryu itself only exists in the Ryu venv)."""
import os, sys, types, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import topology


def install_stubs():
    def mod(name, **kw):
        m = types.ModuleType(name); m.__dict__.update(kw); sys.modules[name] = m; return m
    class RyuApp:
        def __init__(self, *a, **k):
            import logging; self.logger = logging.getLogger("t")
    mod("ryu"); mod("ryu.base"); mod("ryu.base.app_manager", RyuApp=RyuApp)
    mod("ryu.controller"); mod("ryu.controller.ofp_event", EventOFPSwitchFeatures=1, EventOFPStateChange=2, EventOFPPacketIn=3)
    mod("ryu.controller.handler", CONFIG_DISPATCHER=1, MAIN_DISPATCHER=2, DEAD_DISPATCHER=3, set_ev_cls=lambda *a, **k: (lambda f: f))
    mod("ryu.lib"); mod("ryu.lib.hub", spawn=lambda *a, **k: None)
    pk = mod("ryu.lib.packet"); pk.packet = mod("ryu.lib.packet.packet", Packet=object)
    pk.ethernet = mod("ryu.lib.packet.ethernet", ethernet=object); pk.ether_types = mod("ryu.lib.packet.ether_types", ETH_TYPE_LLDP=0x88cc)
    mod("ryu.ofproto"); mod("ryu.ofproto.ofproto_v1_3", OFP_VERSION=4)


install_stubs()
sent = []


class P:
    def OFPMatch(self, **k): return ("match", tuple(sorted(k.items())))
    def OFPActionSetQueue(self, q): return ("queue", q)
    def OFPActionOutput(self, p, *a): return ("out", p)
    def OFPInstructionActions(self, t, acts): return ("inst", tuple(acts))
    def OFPFlowMod(self, **k): return ("flowmod", k)


class OFP:
    OFPIT_APPLY_ACTIONS = 4; OFPFC_DELETE_STRICT = 4; OFPFC_DELETE = 3; OFPP_ANY = 0xfffffff; OFPG_ANY = 0xfffffff
    OFPTT_ALL = 0xff; OFPP_CONTROLLER = 0xfffffffd; OFPCML_NO_BUFFER = 0xffff


class DP:
    id = 1; ofproto = OFP(); ofproto_parser = P()
    def send_msg(self, m): sent.append(m)


import controller as c


class ControllerTests(unittest.TestCase):
    def setUp(self):
        sent.clear()
        self.app = c.GameController(); self.app.dp = DP()

    def test_constants_match_topology(self):
        self.assertEqual(c.SERVER_SWITCH_PORT, topology.SWITCH_PORTS["server"])
        self.assertEqual(c.SERVER_IP, topology.HOST_IPS["server"])
        for host in topology.CLIENT_HOSTS:
            self.assertEqual(c.CLIENT_PORTS[topology.HOST_IPS[host]], topology.SWITCH_PORTS[host])

    def test_game_rule_uses_priority_queue(self):
        self.app.install_game_forward()
        m = sent[0][1]
        self.assertEqual(m["priority"], 200)
        self.assertIn(("queue", 1), m["instructions"][0][1]); self.assertIn(("out", 5), m["instructions"][0][1])

    def test_background_uses_best_effort(self):
        self.app.install_background()
        m = sent[0][1]
        self.assertEqual(m["priority"], 100); self.assertIn(("queue", 0), m["instructions"][0][1])

    def test_block_unblock(self):
        r = self.app.handle_policy({"cmd": "block", "ip": "10.0.0.1"})
        self.assertTrue(r["ok"]); self.assertEqual(len(sent), 2)
        self.assertTrue(all(m[1]["priority"] == 300 and m[1]["instructions"] == [] for m in sent))   # drop
        sent.clear()
        self.app.handle_policy({"cmd": "unblock", "ip": "10.0.0.1"})
        self.assertTrue(all(m[1]["command"] == OFP.OFPFC_DELETE_STRICT and m[1]["priority"] == 300 for m in sent))
        self.assertEqual(self.app.handle_policy({"cmd": "status"})["blocked"], [])

    def test_other_clients_untouched_by_block(self):
        self.app.handle_policy({"cmd": "block", "ip": "10.0.0.2"})
        for m in sent:
            self.assertNotIn("10.0.0.1", repr(m)); self.assertIn("10.0.0.2", repr(m))

    def test_qos_toggle_changes_queue(self):
        sent.clear(); self.app.handle_policy({"cmd": "qos", "mode": "off"})
        self.assertIn(("queue", 0), sent[0][1]["instructions"][0][1])
        sent.clear(); self.app.handle_policy({"cmd": "qos", "mode": "on"})
        self.assertIn(("queue", 1), sent[0][1]["instructions"][0][1])

    def test_bad_requests(self):
        self.assertFalse(self.app.handle_policy({"cmd": "block", "ip": "10.9.9.9"})["ok"])
        self.assertFalse(self.app.handle_policy({"cmd": "nope"})["ok"])
        self.assertFalse(self.app.handle_policy({"cmd": "qos", "mode": "x"})["ok"])


if __name__ == "__main__":
    unittest.main()
