# Demo run sheet (about 8 minutes) - everything is clicked in the web page

Start: `./start.sh`, then **Start full demo**.

| # | Say | Do | Evaluator sees |
|---|-----|----|----------------|
| 1 | "UDP game: clients register, send sequence-numbered updates, server forwards them." | *Live* tab. Add a player named `alice` (Register a player) | New dot in arena, new row, RTT/jitter/loss columns |
| 2 | "Players are real processes inside Mininet hosts; Ryu controls the switch." | *SDN & network* tab: show topology and OpenFlow table | Rules at priority 300/200/100/10/0 with counters |
| 3 | "Dynamic SDN: block a client." | Live tab -> **Block** on p1; then SDN tab | p1 disconnected, others fine, red dashed link, priority-300 drop rules, log entry |
| 4 | "Unblock removes only that rule." | **Unblock** | p1 reconnects, rule disappears |
| 5 | "Packet loss detection." | SDN tab -> preset **Lossy** | up/down loss % rise; KPI tile turns amber/red; sequence gaps counted |
| 6 | "Latency and jitter." | preset **Jittery** | RTT and jitter columns and RTT chart change; press **Normal** |
| 7 | "Congestion without prioritisation." | QoS **OFF**, Congestion **Start** (14 Mbit/s) | RTT/jitter of the game rise sharply; queue 0 busy |
| 8 | "Same congestion with SDN prioritisation." | QoS **ON**, Start again | RTT stays low; queue bars show game in queue 1, background in queue 0 |
| 9 | "Application event triggers SDN." | Auto-guard ON, **Start flood** | Flooder is blocked automatically (Log tab) |
| 10 | "Measured results and tests." | *Tests* tab (run before), *Experiments* tab, **Open printable report** | PASS list, results table, charts, computed findings |

Before the evaluation: run **Run all tests** and a full **Run experiments** once so the results exist.
Block works per host IP, so blocking p1 also blocks other players on client1 (say this if asked).
