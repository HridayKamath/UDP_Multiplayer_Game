"""
controller.py - Ryu OpenFlow 1.3 controller (D1 learning switch + D2 dynamic SDN)

Run (Ryu virtual environment):
    ryu-manager --ofp-tcp-listen-port 6653 controller.py

Flow table (higher priority wins):
    300  BLOCK       drop game traffic of one client IP (added/removed at run time)
    200  GAME        UDP dst 9999 -> server port, queue 1 (priority) when QoS is on, else queue 0
    200  GAME RETURN UDP src 9999 -> the client's port
    100  BACKGROUND  UDP dst 10000 -> server port, queue 0 (best effort)
     10  L2 learned  ARP / ping / everything else (D1 learning switch)
      0  TABLE MISS  send to controller

Policy interface: UDP JSON on 127.0.0.1:9998, every request gets a JSON reply
    {"cmd":"block","ip":"10.0.0.1"}   {"cmd":"unblock","ip":"10.0.0.1"}
    {"cmd":"qos","mode":"on"|"off"}   {"cmd":"status"}
"""
import json
import socket

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, DEAD_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.lib import hub
from ryu.lib.packet import packet, ethernet, ether_types
from ryu.ofproto import ofproto_v1_3

GAME_PORT = 9999
BG_PORT = 10000
POLICY_PORT = 9998

SERVER_IP = "10.0.0.10"
SERVER_SWITCH_PORT = 5                     # must match topology.py
CLIENT_PORTS = {"10.0.0.1": 1, "10.0.0.2": 2, "10.0.0.3": 3, "10.0.0.4": 4}

PRIO_BLOCK, PRIO_GAME, PRIO_BG, PRIO_L2, PRIO_MISS = 300, 200, 100, 10, 0
QUEUE_BEST_EFFORT, QUEUE_PRIORITY = 0, 1


class GameController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(GameController, self).__init__(*args, **kwargs)
        self.dp = None
        self.mac_to_port = {}
        self.blocked = set()
        self.qos_on = True
        hub.spawn(self.policy_loop)

    # ---------------- helpers ----------------
    def add_flow(self, priority, match, actions, idle_timeout=0):
        ofp, parser = self.dp.ofproto, self.dp.ofproto_parser
        inst = []                                        # no instructions = DROP
        if actions:
            inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        self.dp.send_msg(parser.OFPFlowMod(datapath=self.dp, priority=priority, match=match,
                                           instructions=inst, idle_timeout=idle_timeout))

    def delete_flow_strict(self, priority, match):
        ofp, parser = self.dp.ofproto, self.dp.ofproto_parser
        self.dp.send_msg(parser.OFPFlowMod(datapath=self.dp, command=ofp.OFPFC_DELETE_STRICT,
                                           priority=priority, match=match,
                                           out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY))

    def game_queue(self):
        return QUEUE_PRIORITY if self.qos_on else QUEUE_BEST_EFFORT

    # ---------------- switch connects ----------------
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def on_switch_features(self, ev):
        self.dp = ev.msg.datapath
        ofp, parser = self.dp.ofproto, self.dp.ofproto_parser
        self.mac_to_port = {}
        self.logger.info("Switch %s connected - installing flows (qos=%s)",
                         self.dp.id, "on" if self.qos_on else "off")
        self.dp.send_msg(parser.OFPFlowMod(datapath=self.dp, table_id=ofp.OFPTT_ALL,
                                           command=ofp.OFPFC_DELETE, out_port=ofp.OFPP_ANY,
                                           out_group=ofp.OFPG_ANY, match=parser.OFPMatch()))
        self.add_flow(PRIO_MISS, parser.OFPMatch(),
                      [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)])
        self.install_game_forward()
        self.install_game_return()
        self.install_background()
        for ip in sorted(self.blocked):
            self.install_block(ip)

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def on_state_change(self, ev):
        if ev.state == DEAD_DISPATCHER and ev.datapath is self.dp:
            self.logger.info("Switch disconnected")
            self.dp = None

    def install_game_forward(self):
        parser = self.dp.ofproto_parser
        match = parser.OFPMatch(eth_type=0x0800, ip_proto=17, ipv4_dst=SERVER_IP, udp_dst=GAME_PORT)
        self.add_flow(PRIO_GAME, match, [parser.OFPActionSetQueue(self.game_queue()),
                                         parser.OFPActionOutput(SERVER_SWITCH_PORT)])

    def install_game_return(self):
        parser = self.dp.ofproto_parser
        for ip, port in CLIENT_PORTS.items():
            match = parser.OFPMatch(eth_type=0x0800, ip_proto=17, ipv4_dst=ip, udp_src=GAME_PORT)
            self.add_flow(PRIO_GAME, match, [parser.OFPActionOutput(port)])

    def install_background(self):
        parser = self.dp.ofproto_parser
        match = parser.OFPMatch(eth_type=0x0800, ip_proto=17, ipv4_dst=SERVER_IP, udp_dst=BG_PORT)
        self.add_flow(PRIO_BG, match, [parser.OFPActionSetQueue(QUEUE_BEST_EFFORT),
                                       parser.OFPActionOutput(SERVER_SWITCH_PORT)])

    # ---------------- dynamic block / unblock ----------------
    def block_matches(self, ip):
        parser = self.dp.ofproto_parser
        return [parser.OFPMatch(eth_type=0x0800, ip_proto=17, ipv4_src=ip, udp_dst=GAME_PORT),
                parser.OFPMatch(eth_type=0x0800, ip_proto=17, ipv4_dst=ip, udp_src=GAME_PORT)]

    def install_block(self, ip):
        for match in self.block_matches(ip):
            self.add_flow(PRIO_BLOCK, match, [])

    def remove_block(self, ip):
        for match in self.block_matches(ip):
            self.delete_flow_strict(PRIO_BLOCK, match)

    # ---------------- policy interface ----------------
    def handle_policy(self, req):
        cmd = req.get("cmd")
        if cmd in ("block", "unblock"):
            ip = req.get("ip")
            if ip not in CLIENT_PORTS:
                return {"ok": False, "msg": "unknown client ip %r (allowed: %s)"
                        % (ip, ", ".join(sorted(CLIENT_PORTS)))}
            if cmd == "block":
                if ip in self.blocked:
                    return {"ok": True, "msg": "%s was already blocked" % ip}
                self.blocked.add(ip)
                if self.dp is not None:
                    self.install_block(ip)
                self.logger.info("POLICY: BLOCK game traffic of %s (priority %d drop)", ip, PRIO_BLOCK)
                return {"ok": True, "msg": "blocked %s: priority-%d drop rules installed" % (ip, PRIO_BLOCK)}
            if ip not in self.blocked:
                return {"ok": True, "msg": "%s was not blocked" % ip}
            self.blocked.discard(ip)
            if self.dp is not None:
                self.remove_block(ip)
            self.logger.info("POLICY: UNBLOCK %s (drop rules removed)", ip)
            return {"ok": True, "msg": "unblocked %s: drop rules removed" % ip}

        if cmd == "qos":
            mode = req.get("mode")
            if mode not in ("on", "off"):
                return {"ok": False, "msg": "mode must be on or off"}
            self.qos_on = (mode == "on")
            if self.dp is not None:
                self.install_game_forward()          # same match + priority replaces the old rule
            self.logger.info("POLICY: QoS %s (game traffic -> queue %d)", mode, self.game_queue())
            return {"ok": True, "msg": "QoS %s: game traffic uses queue %d" % (mode, self.game_queue())}

        if cmd == "status":
            return {"ok": True, "switch_connected": self.dp is not None,
                    "blocked": sorted(self.blocked), "qos": "on" if self.qos_on else "off",
                    "game_queue": self.game_queue()}
        return {"ok": False, "msg": "unknown cmd %r" % cmd}

    def policy_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", POLICY_PORT))
        self.logger.info("Policy listener on 127.0.0.1:%d", POLICY_PORT)
        while True:
            data, addr = sock.recvfrom(4096)
            try:
                reply = self.handle_policy(json.loads(data.decode("utf-8")))
            except Exception as e:                  # a bad request must never kill the loop
                reply = {"ok": False, "msg": "bad request: %s" % e}
            sock.sendto(json.dumps(reply).encode("utf-8"), addr)

    # ---------------- learning switch (ARP, ping, ...) ----------------
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def on_packet_in(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        in_port = msg.match["in_port"]
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]
        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return
        self.mac_to_port[eth.src] = in_port
        if eth.dst in self.mac_to_port:
            out_port = self.mac_to_port[eth.dst]
            match = parser.OFPMatch(in_port=in_port, eth_dst=eth.dst, eth_src=eth.src)
            self.add_flow(PRIO_L2, match, [parser.OFPActionOutput(out_port)], idle_timeout=60)
        else:
            out_port = ofp.OFPP_FLOOD
        data = msg.data if msg.buffer_id == ofp.OFP_NO_BUFFER else None
        dp.send_msg(parser.OFPPacketOut(datapath=dp, buffer_id=msg.buffer_id, in_port=in_port,
                                        actions=[parser.OFPActionOutput(out_port)], data=data))
