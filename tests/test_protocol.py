"""Game server protocol test over loopback (real UDP sockets, real server code)."""
import json, os, socket, sys, tempfile, threading, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from server import GameServer


class ServerProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = os.path.join(tempfile.mkdtemp(), "state.json")
        cls.server = GameServer(19991, 19992, cls.state, True)
        threading.Thread(target=cls.server.run, daemon=True).start()
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        cls.server.running = False; time.sleep(0.2)

    def client(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(1.0); return s

    def send(self, s, obj):
        s.sendto(json.dumps(obj).encode(), ("127.0.0.1", 19991))

    def recv_type(self, s, kind):
        end = time.time() + 1.5
        while time.time() < end:
            try:
                m = json.loads(s.recvfrom(4096)[0].decode())
            except socket.timeout:
                continue
            if m.get("type") == kind:
                return m
        return None

    def player(self, pid):
        time.sleep(0.7)
        with open(self.state) as f:
            return {p["id"]: p for p in json.load(f)["players"]}.get(pid)

    def test_register_ack_and_loss_accounting(self):
        a, b = self.client(), self.client()
        self.send(a, {"type": "register", "player_id": "ta", "session": 1})
        self.assertEqual(self.recv_type(a, "registered")["player_id"], "ta")
        self.send(b, {"type": "register", "player_id": "tb", "session": 2})
        self.recv_type(b, "registered")
        t0 = time.time()
        for seq in (0, 1, 2, 4, 4, 3):          # 3 is late, 4 is duplicated
            self.send(a, {"type": "update", "player_id": "ta", "session": 1, "seq": seq, "x": seq, "y": 1, "timestamp": t0})
        ack = self.recv_type(a, "ack")
        self.assertEqual(ack["timestamp"], t0)  # timestamp echoed for the RTT measurement
        fwd = self.recv_type(b, "update")       # D1 behaviour: forwarded to the other player
        self.assertEqual(fwd["player_id"], "ta")
        p = self.player("ta")
        self.assertEqual((p["up_received"], p["up_lost"], p["duplicates"], p["out_of_order"]), (5, 0, 1, 1))

    def test_missing_updates_counted(self):
        a = self.client()
        self.send(a, {"type": "register", "player_id": "tc", "session": 5}); self.recv_type(a, "registered")
        for seq in (0, 1, 5):
            self.send(a, {"type": "update", "player_id": "tc", "session": 5, "seq": seq, "x": 1, "y": 1, "timestamp": time.time()})
        p = self.player("tc")
        self.assertEqual((p["up_received"], p["up_lost"]), (3, 3))

    def test_disconnect_detected_after_bye(self):
        a = self.client()
        self.send(a, {"type": "register", "player_id": "td", "session": 9}); self.recv_type(a, "registered")
        self.send(a, {"type": "update", "player_id": "td", "session": 9, "seq": 0, "x": 1, "y": 1, "timestamp": time.time()})
        self.assertEqual(self.player("td")["status"], "connected")
        self.send(a, {"type": "bye", "player_id": "td"})
        self.assertEqual(self.player("td")["status"], "disconnected")

    def test_garbage_is_ignored(self):
        a = self.client()
        a.sendto(b"not json", ("127.0.0.1", 19991))
        self.send(a, {"type": "update", "player_id": "bad id!", "seq": 1})
        self.send(a, {"type": "register", "player_id": "te", "session": 1})
        self.assertIsNotNone(self.recv_type(a, "registered"))   # server still alive


if __name__ == "__main__":
    unittest.main()
