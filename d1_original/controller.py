
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types, ipv4, udp


# MAIN CONTROLLER CLASS: Uses Ryu to manage the OpenFlow switch
class GameController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    # INITIALIZATION: Set up MAC learning and start the controller
    def __init__(self, *args, **kwargs):
        super(GameController, self).__init__(*args, **kwargs)
        self.mac_to_port = {}
        self.logger.info("Game Controller Started")

    # ADD FLOW: Create and install a forwarding rule in the switch
    def add_flow(self, datapath, priority, match, actions, idle_timeout=0):
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto

        instructions = [
            parser.OFPInstructionActions(
                ofproto.OFPIT_APPLY_ACTIONS, actions
            )
        ]

        flow_mod = parser.OFPFlowMod(
            datapath=datapath,
            priority=priority,
            match=match,
            instructions=instructions,
            idle_timeout=idle_timeout
        )

        datapath.send_msg(flow_mod)

    # SWITCH CONNECTION: Install a default rule sending unknown packets to Ryu
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto

        match = parser.OFPMatch()
        actions = [
            parser.OFPActionOutput(
                ofproto.OFPP_CONTROLLER,
                ofproto.OFPCML_NO_BUFFER
            )
        ]

        self.add_flow(datapath, 0, match, actions)

        self.logger.info("Switch connected: %s", datapath.id)
        self.logger.info("Table-miss flow installed")

    # PACKET-IN HANDLER: Learn MAC addresses and decide where packets go
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto

        in_port = msg.match["in_port"]

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)

        # IGNORE: Skip invalid Ethernet packets and LLDP discovery packets
        if eth is None or eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        dst = eth.dst
        src = eth.src
        dpid = datapath.id

        # MAC LEARNING: Remember which switch port each source MAC uses
        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][src] = in_port

        # FORWARDING: Use the known destination port, or flood if unknown
        out_port = self.mac_to_port[dpid].get(dst, ofproto.OFPP_FLOOD)
        actions = [parser.OFPActionOutput(out_port)]

        # PACKET IDENTIFICATION: Check whether this is IPv4 UDP game traffic
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        udp_pkt = pkt.get_protocol(udp.udp)

        if ip_pkt is not None and udp_pkt is not None:
            if udp_pkt.dst_port == 9999:
                self.logger.info(
                    "Game UDP traffic detected: %s:%s -> %s:%s",
                    ip_pkt.src,
                    udp_pkt.src_port,
                    ip_pkt.dst,
                    udp_pkt.dst_port
                )

                # FLOW INSTALLATION: Add a specific rule for game packets
                if dst in self.mac_to_port[dpid]:
                    match = parser.OFPMatch(
                        in_port=in_port,
                        eth_type=ether_types.ETH_TYPE_IP,
                        ip_proto=17,
                        ipv4_src=ip_pkt.src,
                        ipv4_dst=ip_pkt.dst,
                        udp_dst=9999
                    )

                    self.add_flow(
                        datapath,
                        100,
                        match,
                        actions,
                        idle_timeout=30
                    )

                    self.logger.info(
                        "Game traffic flow installed: output port %s",
                        out_port
                    )

        # PACKET-OUT: Forward the current packet using the selected action
        if msg.buffer_id != ofproto.OFP_NO_BUFFER:
            out = parser.OFPPacketOut(
                datapath=datapath,
                buffer_id=msg.buffer_id,
                in_port=in_port,
                actions=actions
            )
        else:
            out = parser.OFPPacketOut(
                datapath=datapath,
                buffer_id=ofproto.OFP_NO_BUFFER,
                in_port=in_port,
                actions=actions,
                data=msg.data
            )

        datapath.send_msg(out)
