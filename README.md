# Multiplayer Game Communication Network over SDN

**Computer Networks:**
UDP multiplayer game with an SDN controller that prioritises game traffic, built with Mininet, Open vSwitch and the Ryu controller, and operated from a web dashboard.

---



---

## 1. Introduction

Online games send small state updates (player positions) many times per second. These updates are time-sensitive: a late update is useless, so games use **UDP** instead of TCP. UDP has no retransmission and no congestion control, which means game traffic suffers badly when the network is congested.

This project builds a small multiplayer game over UDP and shows how a **Software Defined Network (SDN)** can protect it:

- Players register with a central server and send sequence-numbered position updates.
- The server and clients measure **packet loss, latency (RTT) and jitter**.
- A Ryu controller programs an Open vSwitch switch so that **game traffic gets a priority queue** on the congested link.
- The controller also reacts at run time: it can **block and unblock clients**, switch prioritisation on and off, and automatically block a client that floods the server.
- A **web dashboard** controls everything and runs automatic experiments that compare the network **with and without** prioritisation.

## 2. Features

- **UDP game**: register multiple players (up to 12, custom IDs), position updates, server forwards updates to other players.
- **Sequence numbers**: every update is numbered, so the receiver detects lost, late and duplicate packets.
- **Measurements**: upstream loss (server side), downstream loss (client side), RTT, jitter, updates per second.
- **Traffic prioritisation**: OVS HTB queues on the server-facing port; game traffic goes to a high-priority queue.
- **Dynamic SDN**: block/unblock a client, QoS on/off, and an auto-guard that blocks a flooding client.
- **Congestion and impairment testing**: background UDP load, plus delay, jitter and loss on client links.
- **Experiments**: six automatic scenarios, averaged over repeats, with charts, a computed summary, a CSV file and a printable report.
- **Tests**: 23 unit tests and 12 live tests that can be run from the web UI.

## 3. System architecture

```
 client1 10.0.0.1 --eth1\
 client2 10.0.0.2 --eth2 \                                   UDP 9999 game
 client3 10.0.0.3 --eth3  +-- OVS s1 -- eth5 (BOTTLENECK, HTB queues) -- server 10.0.0.10
 client4 10.0.0.4 --eth4 /       |                                         UDP 10000 sink
 bg      10.0.0.20 -eth6/        | OpenFlow 1.3 (TCP 6653)
                          Ryu controller  <-- UDP 9998 policy commands -- Web manager
 Web manager (Flask, http://127.0.0.1:8000) starts Ryu and Mininet and polls the switch every second
```

- **Data plane**: Open vSwitch `s1` forwards packets using flow rules.
- **Control plane**: the Ryu controller installs the rules over OpenFlow 1.3.
- **Bottleneck**: the switch-to-server port (`s1-eth5`) is limited to 10 Mbit/s, with two queues: queue 1 for game traffic (priority) and queue 0 for everything else.
- **Web manager**: starts the lab, launches players, applies network conditions and sends policy commands to the controller.

### Flow table (higher priority wins)

| Priority | Match | Action |
|---|---|---|
| 300 | Game UDP from or to a blocked client | Drop (added and removed at run time) |
| 200 | UDP to port 9999 (game) to the server | `set_queue:1` when QoS is on (else `set_queue:0`), output to the server port |
| 200 | UDP from port 9999 to a client | Output to the client's port |
| 100 | UDP to port 10000 (background) | `set_queue:0`, output to the server port |
| 10 | Learned MAC addresses (ARP, ping) | Learning switch, idle timeout 60 s |
| 0 | Table-miss | Send to controller |

### Game protocol (JSON over UDP)

- `register` -> `registered`
- `update` (`player_id`, `seq`, `session`, `timestamp`, `x`, `y`) -> `ack` (echoes the timestamp, used for RTT)
- The server forwards each new update to the other players.
- `bye` when a player leaves. A player silent for 3 s is marked disconnected.

## 4. Files

| File | What it does |
|---|---|
| `start.sh` | One-command launcher: checks tools, prepares Flask, cleans old Mininet state, starts the web manager and opens the browser |
| `manager.py` | Flask web manager: starts Ryu and Mininet, adds players, applies network conditions, polls statistics, runs the auto-guard, serves the API |
| `lab.py` | Mininet backend: netem delay/jitter/loss, queue statistics, flow table, ping; also a local mode without SDN for development |
| `topology.py` | Mininet topology (4 clients, server, background host, one switch), fixed port numbers, OVS QoS (HTB queues) setup |
| `controller.py` | Ryu application: learning switch, proactive game/background rules, block/unblock, QoS on/off, policy listener on UDP 9998 |
| `server.py` | UDP game server: registration, acks, forwarding, loss accounting, disconnect detection, writes live state |
| `client.py` | UDP game client (one player): sequence-numbered updates, RTT and jitter, downstream loss, optional keyboard control |
| `common.py` | Shared constants and the `PlayerTracker` loss/sequence logic |
| `traffic_generator.py` | Rate-controlled background UDP traffic for congestion tests |
| `policy.py` | Command-line tool to send policy commands to the controller (for example `block 10.0.0.1`) |
| `experiments.py` | Runs the six scenarios, aggregates results, draws charts, builds the report |
| `selftest.py` | The 12 live test cases shown in the Tests tab |
| `templates/index.html`, `static/` | The web dashboard (HTML, CSS, JavaScript) |
| `tests/` | Unit tests (no Mininet needed) |
| `docs/` | `DEMO_SCRIPT.md` and `VIVA_NOTES.md` |
| `d1_original/` | The Deliverable 1 files this project builds on |
| `results/` | Generated: `results.csv`, `selftest.json`, `evidence/` |
| `logs/` | Generated: process logs (Ryu, server, players) |

## 5. Requirements

- Ubuntu (tested on Ubuntu 26.04 in VirtualBox) with `sudo` access
- Python 3
- Mininet and Open vSwitch (`ovs-vsctl`, `ovs-ofctl`)
- `tc` (iproute2) for network impairments
- Ryu 4.34 in a virtual environment, by default at `~/cn-project/ryu-env` (as used in Deliverable 1)
- Flask 2.0 or newer (installed automatically into a local `.venv` by `start.sh`; needs internet once)
- A modern web browser

## 6. How to run

```bash
cd ~/cn-d2
chmod +x start.sh
./start.sh
```

The script asks for the sudo password once and opens `http://127.0.0.1:8000`.
If Ryu is installed elsewhere:

```bash
RYU_MANAGER=/path/to/ryu-env/bin/ryu-manager ./start.sh
```

Do not run `sudo mn -c` while the project is running, because it also stops Ryu.

## 7. Using the web dashboard

Click **Start full demo** to start the network, the Ryu controller, the server and 4 players. Then use the five tabs:

| Tab | What it is for |
|---|---|
| **Live game** | Arena, player table (sequence, loss, RTT, jitter, updates/s), register new players, block/unblock, drive a player with the keyboard |
| **SDN & network** | Topology, prioritisation on/off, network conditions (delay, jitter, loss, bandwidth), background traffic, flood and auto-guard, queue statistics, live flow table |
| **Experiments** | Run the six scenarios automatically; results table, charts, findings, CSV download, printable report |
| **Tests** | Run the 12 live test cases and see pass or fail for each |
| **Log** | Event feed and raw logs of Ryu, the server and the players |

## 7.1 Experiments

| Scenario | Delay / jitter | Loss | Background load | QoS |
|---|---|---|---|---|
| Normal network | 0 | 0 | none | - |
| High delay | 40 ms | 0 | none | - |
| Delay + jitter | 40 ± 20 ms | 0 | none | - |
| Packet loss | 0 | 5 % each direction | none | - |
| Congestion, no QoS (baseline) | 0 | 0 | 14 Mbit/s into 10 Mbit/s | off |
| Congestion, QoS | 0 | 0 | 14 Mbit/s into 10 Mbit/s | on |

Every run uses the same 4 players, update rate, duration and bottleneck; only the listed factor changes. Repeats are averaged (mean ± standard deviation). Metrics: average RTT, 95th-percentile RTT, jitter, upstream loss, downstream loss, background throughput and per-queue byte counts.

To reproduce: open the **Experiments** tab, tick the scenarios, set duration and repeats, press **Run experiments**, then open the printable report or download the CSV. Output is saved in `results/`.

## 8. Tests

They cover sequence and loss accounting, the server protocol over real UDP sockets, the controller rules against a Ryu stub, the parsers and the report generator.

The 12 live tests (Tests tab) cover: controller connection, host connectivity, installed flow rules, QoS queues, player registration, RTT and jitter, loss detection, delay, block/unblock, disconnect and re-register, queue classification under congestion, and the auto-guard reaction.


## Team

| Name | SRN | Contribution |
|---|---|---|
| Gyanendra Kumar | PES1UG25CS195 | **Application Functionality:** UDP game server and clients, multi-player registration, packet-loss handling, RTT and jitter measurement. |
| Hriday Kamath | PES1UG25CS207 | **SDN Implementation and Web Interface:** Mininet topology, Ryu controller, QoS queues, dynamic traffic-management policies, and web dashboard. |
| Karthikeya S.H | PES1UG25CS243 | **Performance Evaluation and Documentation:** Experiment design and execution, test cases, performance analysis, result comparison, and documentation. |
