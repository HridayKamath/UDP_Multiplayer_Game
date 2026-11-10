import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import lab


class ParserTests(unittest.TestCase):
    # Sample text in the format printed by ovs-ofctl (written by hand for the parser test)
    FLOWS = """ cookie=0x0, duration=12.3s, table=0, n_packets=0, n_bytes=0, priority=300,udp,nw_src=10.0.0.1,tp_dst=9999 actions=drop
 cookie=0x0, duration=15.1s, table=0, n_packets=420, n_bytes=90000, priority=200,udp,nw_dst=10.0.0.10,tp_dst=9999 actions=set_queue:1,output:5
 cookie=0x0, duration=9.0s, table=0, n_packets=3, n_bytes=294, idle_timeout=60, idle_age=2, priority=10,in_port="s1-eth1",dl_src=aa:bb:cc:00:00:01 actions=output:"s1-eth5"
 cookie=0x0, duration=15.2s, table=0, n_packets=40, n_bytes=3000, priority=0 actions=CONTROLLER:65535"""

    def test_parse_flows(self):
        f = lab.parse_flows(self.FLOWS)
        self.assertEqual([x["priority"] for x in f], [300, 200, 10, 0])
        self.assertEqual(f[0]["actions"], "drop"); self.assertEqual(f[0]["label"], "BLOCK (drop)")
        self.assertEqual((f[1]["packets"], f[1]["bytes"]), (420, 90000))
        self.assertIn("set_queue:1", f[1]["actions"])
        self.assertEqual(f[3]["match"], "(any)")

    def test_parse_queue_stats(self):
        q = lab.parse_queue_stats("  port 5 queue 0: bytes=1000, pkts=10, errors=0\n  port 5 queue 1: bytes=2000, pkts=20, errors=0")
        self.assertEqual(q[0]["bytes"], 1000); self.assertEqual(q[1]["packets"], 20)

    def test_netem(self):
        self.assertEqual(lab.netem_cmd("e", 40, 10, 5), "tc qdisc replace dev e root netem delay 40ms 10ms loss 5%")
        self.assertIn("qdisc del", lab.netem_cmd("e", 0, 0, 0))
        self.assertIn("delay 0ms 8ms", lab.netem_cmd("e", 0, 8, 0))


if __name__ == "__main__":
    unittest.main()
