# Viva notes - answer in your own words

**Why UDP?** Games prefer fresh data to late reliable data; the next update supersedes a lost one. We detect loss, we do not retransmit.

**How is loss detected?** Each update has a sequence number per session. A jump = missing updates (lost); a number that arrives later = out-of-order (counted once, removed from lost); a repeat = duplicate (ignored). Invariant: received + lost = highest + 1.

**Why a session id?** A restarted client starts again at seq 0; the new session tells the server this is a restart, not duplicates.

**How is RTT measured?** The client puts its send time in the update; the server echoes it in an ack; RTT = now - echoed time (same clock, no synchronisation). Jitter = variation of consecutive RTTs (smoothed 0.9/0.1 live, mean absolute difference in experiments).

**What does SDN do here?** Ryu installs rules proactively: game traffic -> priority queue, background -> best-effort queue, learning switch for ARP. It also adds/removes priority-300 drop rules on request and re-installs them if the switch reconnects.

**Why does `set_queue` need more than a rule?** The queue must exist on the output port. `setup_qos` creates an OVS linux-htb QoS with queues 0 and 1 on the server port; the UI shows `queue-stats` counters as proof.

**Where is the congestion?** Switch -> server port limited to 10 Mbit/s; the background host sends 14 Mbit/s. Without QoS game packets wait in the same queue as background packets; with QoS they use the priority queue.

**How does the application influence SDN?** Auto-guard: the server measures updates/s per player; a flooder triggers a block request to the controller (priority-300 drop). Also the QoS toggle and block buttons.

**Why fail-mode secure?** Without a controller the switch would silently act as a normal switch and hide controller failures.

**Fair experiments?** Same players/rate/duration/bottleneck, one factor changed per scenario, repeats averaged with standard deviation; the report text is computed from the numbers.

**Limitations to state:** loss at the end of a session is invisible; blocking is per host IP; netem is applied per direction; results come from an emulated VM network and vary run to run; fixed port numbers are assumed by the controller.

**Be ready to explain:** one packet's path client -> s1 -> server and back; what each priority does; what `dump-flows` and `queue-stats` prove; why `mn -c` kills Ryu.
