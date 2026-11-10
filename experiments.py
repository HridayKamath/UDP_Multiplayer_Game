"""
experiments.py - automatic performance experiments, results table, SVG charts, printable report

One run = 4 players (one per client host) play for `duration` seconds under one scenario.
All numbers come from the clients' own measurements (RTT, jitter, downstream loss) and
from the server (upstream loss, background throughput); nothing is typed in by hand.
"""
import csv
import html
import json
import math
import os
import threading
import time

import topology

# name: (label, delay ms, jitter ms, loss % (each direction), background Mbit/s, qos)
SCENARIOS = [
    ("baseline", "Normal network", 0, 0, 0, 0, "off"),
    ("delay", "High delay (40 ms)", 40, 0, 0, 0, "off"),
    ("jitter", "Delay + jitter (40 +/- 20 ms)", 40, 20, 0, 0, "off"),
    ("loss", "Packet loss (5 % per direction)", 0, 0, 5, 0, "off"),
    ("congestion_noqos", "Congestion, NO prioritisation (baseline)", 0, 0, 0, 14, "off"),
    ("congestion_qos", "Congestion, SDN QoS prioritisation", 0, 0, 0, 14, "on"),
]
SCEN = {s[0]: s for s in SCENARIOS}
FIELDS = ["timestamp", "scenario", "run", "delay_ms", "jitter_cfg_ms", "loss_cfg_pct", "bg_mbps",
          "qos", "bottleneck_mbps", "duration_s", "players", "rtt_avg_ms", "rtt_p95_ms",
          "jitter_ms", "up_loss_pct", "down_loss_pct", "bg_rx_mbps", "q0_mbytes", "q1_mbytes"]
METRICS = ["rtt_avg_ms", "rtt_p95_ms", "jitter_ms", "up_loss_pct", "down_loss_pct", "bg_rx_mbps",
           "q0_mbytes", "q1_mbytes"]


def _mean(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 2) if values else None


class ExperimentRunner:
    def __init__(self, manager):
        self.m = manager
        self.abort = False
        self.status = {"running": False, "msg": "idle", "done": 0, "total": 0, "current": ""}

    # ---------------------------------------------------------- control
    def start(self, names, duration, repeats):
        m = self.m
        if not m.lab.running:
            return {"ok": False, "msg": "Start the network first."}
        if m.lab.name == "local":
            return {"ok": False, "msg": "Experiments need the Mininet lab (dev mode has no SDN)."}
        names = [n for n in (names or [s[0] for s in SCENARIOS]) if n in SCEN]
        if not names:
            return {"ok": False, "msg": "choose at least one scenario"}
        duration = max(10, min(120, duration))
        repeats = max(1, min(5, repeats))
        self.abort = False
        self.status = {"running": True, "msg": "starting", "done": 0, "total": len(names) * repeats,
                       "current": ""}
        m.busy = "experiment"
        threading.Thread(target=self._run_all, args=(names, duration, repeats), daemon=True).start()
        return {"ok": True, "msg": "%d runs started (about %d s)"
                % (len(names) * repeats, len(names) * repeats * (duration + 7))}

    def stop(self):
        self.abort = True
        return {"ok": True, "msg": "stopping after the current step"}

    def _run_all(self, names, duration, repeats):
        m = self.m
        csv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "results.csv")
        try:
            new = not os.path.exists(csv_path)
            with open(csv_path, "a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=FIELDS)
                if new:
                    w.writeheader()
                for name in names:
                    for run_no in range(1, repeats + 1):
                        if self.abort:
                            raise KeyboardInterrupt
                        self.status.update(current="%s (run %d/%d)" % (SCEN[name][1], run_no, repeats),
                                           msg="running")
                        row = self._run_one(name, run_no, duration)
                        w.writerow(row)
                        f.flush()
                        self.status["done"] += 1
            self.status.update(running=False, msg="finished - %d runs saved" % self.status["done"], current="")
            m.log("Experiments finished (%d runs)" % self.status["done"], "ok")
        except KeyboardInterrupt:
            self.status.update(running=False, msg="stopped by user", current="")
            m.log("Experiments stopped", "warn")
        except Exception as e:
            self.status.update(running=False, msg="failed: %s" % e, current="")
            m.log("Experiments failed: %s" % e, "error")
        finally:
            self._restore()
            m.busy = ""

    def _restore(self):
        m = self.m
        try:
            m.stop_everything()
            m.set_conditions(0, 0, 0)
            m.policy({"cmd": "qos", "mode": "on"})
        except Exception:
            pass

    # ---------------------------------------------------------- one run
    def _run_one(self, name, run_no, duration):
        m = self.m
        _n, label, delay, jitter, loss, bg, qos = SCEN[name]
        m.log("Experiment: %s, run %d" % (label, run_no))
        m.stop_everything()
        for ip in m.cache.get("blocked", []):
            m.policy({"cmd": "unblock", "ip": ip})
        m.policy({"cmd": "qos", "mode": qos})
        m.set_conditions(delay, jitter, loss)
        q_before, _ = m.lab.queue_stats()
        m.start_server()
        time.sleep(1.0)
        stat_files = {}
        procs = []
        for i, host in enumerate(topology.CLIENT_HOSTS):
            pid = "p%d" % (i + 1)
            sf = "/tmp/cn_exp_%s.json" % pid
            if os.path.exists(sf):
                os.remove(sf)
            stat_files[pid] = sf
            procs.append(m.spawn(host, ["client.py", pid, "--server", m.lab.server_ip, "--duration",
                                        str(duration), "--stats-file", sf, "--quiet"], "exp_%s.log" % pid))
        if bg:
            self._sleep(2.0)
            procs.append(m.spawn("bg", ["traffic_generator.py", "--dst", m.lab.server_ip, "--mbps", str(bg),
                                        "--duration", str(max(3, duration - 4))], "bg.log"))
            m.bg_info = {"running": True, "mbps": bg, "until": time.time() + duration - 4}
        self._sleep(duration + 3.5)
        state = m.read_state()
        q_after, q_raw = m.lab.queue_stats()
        stats = []
        for sf in stat_files.values():
            try:
                with open(sf) as f:
                    stats.append(json.load(f))
            except (OSError, ValueError):
                pass
        for p in procs:
            m.stop_proc(p)
        players = state.get("players", [])
        got = sum(p["up_received"] for p in players)
        lost = sum(p["up_lost"] for p in players)
        self._evidence(name, run_no, qos, q_raw)
        m.stop_server()
        delta = lambda q: round((q_after.get(q, {}).get("bytes", 0) - q_before.get(q, {}).get("bytes", 0)) / 1e6, 2)
        row = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "scenario": name, "run": run_no,
            "delay_ms": delay, "jitter_cfg_ms": jitter, "loss_cfg_pct": loss, "bg_mbps": bg, "qos": qos,
            "bottleneck_mbps": m.cond["bw"], "duration_s": duration, "players": len(stats),
            "rtt_avg_ms": _mean([s["rtt_avg_ms"] for s in stats]),
            "rtt_p95_ms": _mean([s["rtt_p95_ms"] for s in stats]),
            "jitter_ms": _mean([s["jitter_mean_ms"] for s in stats]),
            "up_loss_pct": round(100.0 * lost / (got + lost), 2) if (got + lost) else None,
            "down_loss_pct": _mean([s["down_loss_pct"] for s in stats]),
            "bg_rx_mbps": (state.get("bg") or {}).get("avg_mbps"),
            "q0_mbytes": delta(0), "q1_mbytes": delta(1),
        }
        m.log("  RTT %s ms, jitter %s ms, loss up %s%% / down %s%%" % (
            row["rtt_avg_ms"], row["jitter_ms"], row["up_loss_pct"], row["down_loss_pct"]))
        return row

    def _sleep(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            if self.abort:
                raise KeyboardInterrupt
            time.sleep(0.25)

    def _evidence(self, name, run_no, qos, q_raw):
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "evidence")
        os.makedirs(d, exist_ok=True)
        _flows, raw = self.m.lab.flows()
        with open(os.path.join(d, "%s_run%d.txt" % (name, run_no)), "w") as f:
            f.write("scenario=%s qos=%s\n\n$ ovs-ofctl -O OpenFlow13 dump-flows s1\n%s\n\n"
                    "$ ovs-ofctl -O OpenFlow13 queue-stats s1 5\n%s\n\n%s\n"
                    % (name, qos, raw, q_raw, topology.qos_report()))


# ================================================================ results
def load_rows(results_dir):
    p = os.path.join(results_dir, "results.csv")
    if not os.path.isfile(p):
        return []
    with open(p) as f:
        return list(csv.DictReader(f))


def _num(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def summarize(rows):
    """-> list of {scenario, label, runs, cfg..., metric: {mean, sd}} in scenario order."""
    out = []
    for name, label, delay, jitter, loss, bg, qos in SCENARIOS:
        group = [r for r in rows if r["scenario"] == name]
        if not group:
            continue
        item = {"scenario": name, "label": label, "runs": len(group), "delay": delay, "jitter": jitter,
                "loss": loss, "bg": bg, "qos": qos}
        for metric in METRICS:
            vals = [v for v in (_num(r.get(metric)) for r in group) if v is not None]
            if vals:
                mean = sum(vals) / len(vals)
                sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)) if len(vals) > 1 else 0.0
                item[metric] = {"mean": round(mean, 2), "sd": round(sd, 2)}
            else:
                item[metric] = None
        out.append(item)
    return out


def _val(item, metric):
    return item[metric]["mean"] if item and item.get(metric) else None


def findings(summary):
    """Plain-language conclusions computed from the measured numbers."""
    by = {s["scenario"]: s for s in summary}
    out = []
    base = by.get("baseline")
    if base:
        out.append("Normal network: average RTT %s ms, jitter %s ms, upstream loss %s %%."
                   % (_val(base, "rtt_avg_ms"), _val(base, "jitter_ms"), _val(base, "up_loss_pct")))
    if base and by.get("delay") and _val(base, "rtt_avg_ms") is not None and _val(by["delay"], "rtt_avg_ms") is not None:
        out.append("Adding 40 ms delay on each direction of the client links raised the average RTT from %s ms to %s ms."
                   % (_val(base, "rtt_avg_ms"), _val(by["delay"], "rtt_avg_ms")))
    if by.get("delay") and by.get("jitter") and _val(by["jitter"], "jitter_ms") is not None:
        out.append("Adding +/-20 ms delay variation raised the measured jitter from %s ms to %s ms."
                   % (_val(by["delay"], "jitter_ms"), _val(by["jitter"], "jitter_ms")))
    if by.get("loss"):
        out.append("With 5 %% packet loss on each direction the server detected %s %% of the client updates as missing "
                   "(sequence-number gaps) and clients detected %s %% of the forwarded updates as missing. "
                   "UDP does not retransmit them." % (_val(by["loss"], "up_loss_pct"), _val(by["loss"], "down_loss_pct")))
    a, b = by.get("congestion_noqos"), by.get("congestion_qos")
    if a and b and _val(a, "rtt_avg_ms") is not None and _val(b, "rtt_avg_ms") is not None:
        ra, rb = _val(a, "rtt_avg_ms"), _val(b, "rtt_avg_ms")
        if ra > 0:
            change = 100.0 * (ra - rb) / ra
            verdict = ("QoS prioritisation reduced the average game RTT under congestion by %.0f %%" % change
                       if change > 5 else
                       "QoS prioritisation did not clearly improve RTT under congestion (change %.0f %%)" % change)
            out.append("%s (from %s ms without QoS to %s ms with QoS). Jitter: %s ms -> %s ms; downstream loss: %s %% -> %s %%."
                       % (verdict, ra, rb, _val(a, "jitter_ms"), _val(b, "jitter_ms"),
                          _val(a, "down_loss_pct"), _val(b, "down_loss_pct")))
    return out


# ================================================================ SVG charts
COLORS = ["var(--c1)", "var(--c2)"]
SHORT = {"baseline": "Normal", "delay": "Delay 40 ms", "jitter": "Delay+jitter", "loss": "Loss 5 %",
         "congestion_noqos": "Congestion|no QoS", "congestion_qos": "Congestion|with QoS"}


def bar_chart(title, unit, items, series):
    """series: list of (legend, metric). Returns an inline SVG string."""
    width, height, left, bottom, top = 640, 300, 56, 64, 38
    values = [_val(i, m) or 0 for i in items for _l, m in series]
    vmax = max(values + [1e-9])
    step = 10 ** math.floor(math.log10(vmax))
    vmax = math.ceil(vmax / step) * step if vmax > 0 else 1
    n = max(1, len(items))
    slot = (width - left - 12) / n
    bw = min(40.0, slot / (len(series) + 1))
    parts = ['<svg viewBox="0 0 %d %d" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="%s">'
             % (width, height, html.escape(title)),
             '<text x="%d" y="20" class="ct">%s (%s)</text>' % (left, html.escape(title), html.escape(unit))]
    for t in range(0, 5):
        y = top + (height - top - bottom) * (1 - t / 4.0)
        parts.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" class="grid"/>' % (left, y, width - 8, y))
        parts.append('<text x="%d" y="%.1f" class="ax" text-anchor="end">%g</text>' % (left - 6, y + 4, round(vmax * t / 4.0, 2)))
    for i, item in enumerate(items):
        x0 = left + slot * i + (slot - bw * len(series)) / 2
        for j, (_legend, metric) in enumerate(series):
            v = _val(item, metric)
            if v is None:
                continue
            h = (height - top - bottom) * v / vmax
            x = x0 + j * bw
            y = height - bottom - h
            parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="2" fill="%s"/>'
                         % (x, y, bw - 3, max(h, 1), COLORS[j % 2]))
            parts.append('<text x="%.1f" y="%.1f" class="vl" text-anchor="middle">%g</text>' % (x + (bw - 3) / 2, y - 4, v))
        label = SHORT.get(item["scenario"], item["label"]).split("|")
        for k, line in enumerate(label[:2]):
            parts.append('<text x="%.1f" y="%d" class="ax" text-anchor="middle">%s</text>'
                         % (left + slot * i + slot / 2, height - bottom + 16 + 13 * k, html.escape(line)))
    for j, (legend, _m) in enumerate(series):
        parts.append('<rect x="%d" y="%d" width="10" height="10" fill="%s"/><text x="%d" y="%d" class="ax">%s</text>'
                     % (width - 190, 8 + 14 * j, COLORS[j % 2], width - 175, 17 + 14 * j, html.escape(legend)))
    parts.append("</svg>")
    return "".join(parts)


def make_charts(summary):
    if not summary:
        return []
    return [
        bar_chart("Round-trip time", "ms", summary, [("average RTT", "rtt_avg_ms"), ("95th percentile", "rtt_p95_ms")]),
        bar_chart("Jitter", "ms", summary, [("mean |RTT(i) - RTT(i-1)|", "jitter_ms")]),
        bar_chart("Packet loss", "%", summary, [("upstream (server detects)", "up_loss_pct"),
                                                 ("downstream (clients detect)", "down_loss_pct")]),
        bar_chart("Data carried per queue during the run", "MB", summary,
                  [("queue 0 (best effort)", "q0_mbytes"), ("queue 1 (game priority)", "q1_mbytes")]),
    ]


# ================================================================ report
CSS = """
:root{--bg:#fff;--fg:#1c2630;--mut:#5b6877;--line:#d9e0e8;--c1:#2c6fbb;--c2:#e08a00;--ok:#1f8a4c;--bad:#b3261e;--card:#f6f8fa}
body{font-family:system-ui,Arial,sans-serif;background:var(--bg);color:var(--fg);max-width:980px;margin:24px auto;padding:0 16px;line-height:1.5}
h1{font-size:24px;margin-bottom:4px}h2{font-size:18px;margin-top:30px;border-bottom:1px solid var(--line);padding-bottom:4px}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{border:1px solid var(--line);padding:5px 8px;text-align:right}
th:first-child,td:first-child{text-align:left}th{background:var(--card)}
.pass{color:var(--ok);font-weight:600}.fail{color:var(--bad);font-weight:600}.skip{color:var(--mut)}
svg{width:100%;max-width:640px;height:auto;margin:8px 0}svg .ct{font-size:13px;font-weight:600;fill:var(--fg)}
svg .ax{font-size:10px;fill:var(--mut)}svg .vl{font-size:10px;fill:var(--fg)}svg .grid{stroke:var(--line)}
.muted{color:var(--mut);font-size:13px}.tests td,.tests th{text-align:left}pre{background:var(--card);padding:10px;overflow:auto;font-size:11px}
@media print{body{margin:0}}
"""


def build_report(results_dir):
    rows = load_rows(results_dir)
    summ = summarize(rows)
    h = ["<!doctype html><html><head><meta charset='utf-8'><title>UDP Game over SDN - Results</title><style>%s</style></head><body>" % CSS,
         "<h1>UDP Multiplayer Game over SDN - Performance Report</h1>",
         "<p class='muted'>Generated %s from results/results.csv. Mininet + Open vSwitch + Ryu (OpenFlow 1.3). "
         "Use your browser's Print to save as PDF.</p>" % time.strftime("%Y-%m-%d %H:%M"),
         "<h2>Setup</h2><p>4 players (one per client host) send 10 position updates per second to a UDP game server. "
         "The switch-to-server link is the bottleneck (OVS HTB queues: queue 1 = game, priority; queue 0 = best effort). "
         "Delay, jitter and loss are applied with netem on both directions of the client links. "
         "Congestion is 14 Mbit/s of background UDP (port 10000) into the bottleneck. "
         "RTT = ack arrival time - send timestamp (echoed by the server); jitter = mean |RTT(i) - RTT(i-1)|.</p>"]
    if not summ:
        h.append("<p>No experiment results yet. Run the experiments from the web UI.</p>")
    else:
        h.append("<h2>Results (mean of runs, +/- standard deviation)</h2><table><tr><th>Scenario</th><th>Runs</th>"
                 "<th>RTT avg ms</th><th>RTT p95 ms</th><th>Jitter ms</th><th>Upstream loss %</th>"
                 "<th>Downstream loss %</th><th>BG received Mbit/s</th></tr>")
        for s in summ:
            def cell(m):
                v = s.get(m)
                return "-" if not v else ("%g &plusmn; %g" % (v["mean"], v["sd"]) if s["runs"] > 1 else "%g" % v["mean"])
            h.append("<tr><td>%s</td><td>%d</td>%s</tr>" % (html.escape(s["label"]), s["runs"], "".join(
                "<td>%s</td>" % cell(m) for m in ("rtt_avg_ms", "rtt_p95_ms", "jitter_ms", "up_loss_pct",
                                                  "down_loss_pct", "bg_rx_mbps"))))
        h.append("</table><h2>Charts</h2>")
        h.extend(make_charts(summ))
        h.append("<h2>Findings (computed from the measurements)</h2><ul>")
        h.extend("<li>%s</li>" % html.escape(t) for t in findings(summ))
        h.append("</ul>")
    p = os.path.join(results_dir, "selftest.json")
    if os.path.isfile(p):
        try:
            with open(p) as f:
                t = json.load(f)
            h.append("<h2>Test cases (%s)</h2><table class='tests'><tr><th>Test</th><th>Requirement</th><th>Result</th><th>Detail</th></tr>" % html.escape(t.get("time", "")))
            for r in t.get("results", []):
                h.append("<tr><td>%s</td><td>%s</td><td class='%s'>%s</td><td style='text-align:left'>%s</td></tr>"
                         % (html.escape(r["name"]), html.escape(r["req"]), r["status"], r["status"].upper(), html.escape(r["detail"])))
            h.append("</table>")
        except (OSError, ValueError):
            pass
    ev = os.path.join(results_dir, "evidence")
    if os.path.isdir(ev):
        files = sorted(f for f in os.listdir(ev) if f.startswith("congestion_qos"))
        if files:
            with open(os.path.join(ev, files[-1])) as f:
                h.append("<h2>Evidence: switch state during a QoS run (%s)</h2><pre>%s</pre>" % (files[-1], html.escape(f.read()[:3500])))
    h.append("</body></html>")
    return "".join(h)
