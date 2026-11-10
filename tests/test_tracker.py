import os, random, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common import PlayerTracker


class TrackerTests(unittest.TestCase):
    def test_gaps_duplicates_out_of_order(self):
        t = PlayerTracker()
        out = [t.on_packet(1, s) for s in [0, 1, 2, 4, 4, 3, 3, 7, 8, 11]]
        self.assertEqual(out, ["new", "new", "new", "new", "duplicate", "late", "duplicate", "new", "new", "new"])
        # unique received: 0,1,2,3,4,7,8,11 = 8 ; lost: 5,6,9,10 = 4 ; highest = 11
        self.assertEqual((t.received, t.lost, t.highest, t.duplicates, t.out_of_order), (8, 4, 11, 2, 1))
        self.assertEqual(t.received + t.lost, t.highest + 1)

    def test_first_packet_not_zero_counts_earlier_ones_lost(self):
        t = PlayerTracker(); t.on_packet(1, 3)
        self.assertEqual((t.lost, t.received), (3, 1))

    def test_joining_midstream_does_not_count_earlier_updates_as_lost(self):
        t = PlayerTracker()
        for s in (300, 301, 303):                       # observer joined when sender was at 300
            t.on_packet(1, s, join_midstream=True)
        self.assertEqual((t.received, t.lost), (3, 1))  # only 302 is really missing

    def test_restart_with_new_session_resets(self):
        t = PlayerTracker()
        for s in range(5): t.on_packet(10, s)
        t.on_packet(20, 0)
        self.assertEqual((t.received, t.lost, t.highest, t.restarts), (1, 0, 0, 1))

    def test_invariant_random(self):
        random.seed(3)
        t = PlayerTracker(); seqs = list(range(500)); random.shuffle(seqs)
        seqs = [s for s in seqs if random.random() > 0.1]; seqs += random.sample(seqs, 30)
        for s in seqs: t.on_packet(7, s)
        self.assertEqual(t.received + t.lost, t.highest + 1)
        self.assertEqual(t.received, len(set(seqs)))

    def test_loss_percent(self):
        t = PlayerTracker()
        for s in (0, 1, 5): t.on_packet(1, s)           # 2,3,4 lost of 6
        self.assertAlmostEqual(t.loss_pct(), 50.0)


if __name__ == "__main__":
    unittest.main()
