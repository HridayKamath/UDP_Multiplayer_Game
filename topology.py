"""
topology.py - Mininet topology and OVS QoS queues

Layout (switch port numbers are FIXED; controller.py relies on them):

    client1 10.0.0.1  --- s1-eth1 \\
    client2 10.0.0.2  --- s1-eth2  \\
    client3 10.0.0.3  --- s1-eth3   +-- s1 (OVS, OpenFlow 1.3) --- s1-eth5 --- server 10.0.0.10
    client4 10.0.0.4  --- s1-eth4  /                                 ^
    bg      10.0.0.20 --- s1-eth6 /                       BOTTLENECK (OVS HTB queues)

Ryu controller: 127.0.0.1:6653.
Players run on client1..client4 (several players may share a host).
The bg host only sends background traffic (congestion).
"""
import subprocess

HOST_IPS = {
    "client1": "10.0.0.1", "client2": "10.0.0.2", "client3": "10.0.0.3",
    "client4": "10.0.0.4", "server": "10.0.0.10", "bg": "10.0.0.20",
}
SWITCH_PORTS = {"client1": 1, "client2": 2, "client3": 3, "client4": 4, "server": 5, "bg": 6}
CLIENT_HOSTS = ["client1", "client2", "client3", "client4"]
BOTTLENECK_IF = "s1-eth5"
RYU_IP = "127.0.0.1"
RYU_PORT = 6653


def build_network():
    """Create the Mininet network (not started)."""
    from mininet.net import Mininet
    from mininet.node import OVSKernelSwitch, RemoteController
    from mininet.link import Link

    net = Mininet(controller=None, switch=OVSKernelSwitch, link=Link,
                  autoSetMacs=True, build=False)
    net.addController("c0", controller=RemoteController, ip=RYU_IP, port=RYU_PORT)
    # fail-mode secure: no controller = no forwarding (a controller problem is never hidden)
    s1 = net.addSwitch("s1", protocols="OpenFlow13", failMode="secure")
    for name, ip in HOST_IPS.items():
        net.addHost(name, ip=ip + "/24")
    for name, port in SWITCH_PORTS.items():
        net.addLink(net.get(name), s1, port2=port)
    return net


def run(cmd):
    """Run a shell command, return (ok, output)."""
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return p.returncode == 0, (p.stdout + p.stderr).strip()


def setup_qos(bw_mbit):
    """Attach an HTB QoS object with two queues to the bottleneck port.

    queue 1 = game traffic   (priority 1, guaranteed 60 %, may use all)
    queue 0 = best effort    (priority 2, guaranteed 10 %, may use all)
    """
    bw = int(bw_mbit * 1000000)
    q1_min, q0_min = int(bw * 0.6), int(bw * 0.1)
    run("ovs-vsctl --if-exists clear port %s qos" % BOTTLENECK_IF)
    run("ovs-vsctl --all destroy qos")
    run("ovs-vsctl --all destroy queue")
    cmd = (
        "ovs-vsctl -- set port %(p)s qos=@newqos "
        "-- --id=@newqos create qos type=linux-htb other-config:max-rate=%(bw)d "
        "queues:0=@q0 queues:1=@q1 "
        "-- --id=@q0 create queue other-config:min-rate=%(q0)d other-config:max-rate=%(bw)d "
        "other-config:priority=2 "
        "-- --id=@q1 create queue other-config:min-rate=%(q1)d other-config:max-rate=%(bw)d "
        "other-config:priority=1"
    ) % {"p": BOTTLENECK_IF, "bw": bw, "q0": q0_min, "q1": q1_min}
    ok, out = run(cmd)
    return ok, out


def qos_report():
    """Text proving which queues exist and where they are attached."""
    parts = []
    for cmd in ("ovs-vsctl get port %s qos" % BOTTLENECK_IF, "ovs-vsctl list qos",
                "ovs-vsctl list queue", "tc -s class show dev %s" % BOTTLENECK_IF):
        _ok, out = run(cmd)
        parts.append("$ %s\n%s\n" % (cmd, out or "(no output)"))
    return "\n".join(parts)
