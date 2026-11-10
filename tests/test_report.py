"""Report/summary code with SYNTHETIC rows (test fixture to check rendering only - not measurements)."""
import csv, os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import experiments as E


def fake_rows():
    base = dict(timestamp="t", run=1, delay_ms=0, jitter_cfg_ms=0, loss_cfg_pct=0, bg_mbps=0, qos="off", bottleneck_mbps=10,
                duration_s=20, players=4, rtt_p95_ms=1, jitter_ms=1, up_loss_pct=0, down_loss_pct=0, bg_rx_mbps=0, q0_mbytes=0, q1_mbytes=0)
    return [dict(base, scenario="baseline", rtt_avg_ms=2), dict(base, scenario="baseline", rtt_avg_ms=4, run=2),
            dict(base, scenario="congestion_noqos", rtt_avg_ms=800), dict(base, scenario="congestion_qos", rtt_avg_ms=8)]


class ReportTests(unittest.TestCase):
    def test_summary_mean_and_sd(self):
        s = {i["scenario"]: i for i in E.summarize(fake_rows())}
        self.assertEqual(s["baseline"]["rtt_avg_ms"]["mean"], 3.0)
        self.assertAlmostEqual(s["baseline"]["rtt_avg_ms"]["sd"], 1.41, places=2)

    def test_findings_do_not_claim_improvement_without_one(self):
        rows = fake_rows(); rows[3]["rtt_avg_ms"] = 790
        text = " ".join(E.findings(E.summarize(rows)))
        self.assertIn("did not clearly improve", text)

    def test_charts_and_report_build(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "results.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=E.FIELDS); w.writeheader(); w.writerows(fake_rows())
        self.assertEqual(len(E.make_charts(E.summarize(E.load_rows(d)))), 4)
        self.assertIn("<svg", E.build_report(d))
        self.assertIn("No experiment results yet", E.build_report(tempfile.mkdtemp()))


if __name__ == "__main__":
    unittest.main()
