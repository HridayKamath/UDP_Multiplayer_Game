#!/usr/bin/env python3
"""
policy.py - send a policy command to the Ryu controller

Run in the Ubuntu terminal (NOT inside a Mininet host):
    python3 policy.py block 10.0.0.1
    python3 policy.py unblock 10.0.0.1
    python3 policy.py qos on        (game traffic -> priority queue 1)
    python3 policy.py qos off       (game traffic -> best-effort queue 0)
    python3 policy.py status
"""
import json
import socket
import sys

CONTROLLER = ("127.0.0.1", 9998)


def send_policy(request, timeout=1.5):
    """Send one JSON request, return the controller's reply as a dict."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(json.dumps(request).encode("utf-8"), CONTROLLER)
        data, _ = sock.recvfrom(4096)
        return json.loads(data.decode("utf-8"))
    except socket.timeout:
        return {"ok": False, "msg": "no reply from controller (is Ryu running?)"}
    except (OSError, ValueError) as e:
        return {"ok": False, "msg": "error: %s" % e}
    finally:
        sock.close()


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)
    cmd = args[0]
    if cmd in ("block", "unblock") and len(args) == 2:
        request = {"cmd": cmd, "ip": args[1]}
    elif cmd == "qos" and len(args) == 2:
        request = {"cmd": "qos", "mode": args[1]}
    elif cmd == "status" and len(args) == 1:
        request = {"cmd": "status"}
    else:
        print(__doc__)
        sys.exit(1)
    reply = send_policy(request)
    print(json.dumps(reply, indent=2))
    sys.exit(0 if reply.get("ok") else 2)


if __name__ == "__main__":
    main()
