#!/usr/bin/env python3

from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel, info

RYU_IP = "127.0.0.1"
RYU_PORT = 6653

def main():
    net = Mininet(
        controller=None,
        switch=OVSSwitch,
        link=TCLink,
        autoSetMacs=True,
        build=False
    )

    net.addController(
        "c0",
        controller=RemoteController,
        ip=RYU_IP,
        port=RYU_PORT
    )

    client1 = net.addHost("client1", ip="10.0.0.1/24")
    client2 = net.addHost("client2", ip="10.0.0.2/24")
    server = net.addHost("server", ip="10.0.0.10/24")

    s1 = net.addSwitch("s1", protocols="OpenFlow13")

    net.addLink(client1, s1)
    net.addLink(client2, s1)
    net.addLink(server, s1)

    info("*** Building and starting topology\n")
    net.build()
    net.start()

    info("*** Hosts: client1=10.0.0.1, client2=10.0.0.2, server=10.0.0.10\n")
    info("*** Enter 'pingall' to test connectivity\n")
    info("*** Start server: server python3 server.py\n")
    info("*** Start clients: client1 python3 client.py p1\n")
    info("***              client2 python3 client.py p2\n")

    CLI(net)
    net.stop()

if __name__ == "__main__":
    setLogLevel("info")
    main()
