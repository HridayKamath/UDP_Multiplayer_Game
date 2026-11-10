import socket
import json
import time
import threading
import random
import sys

SERVER_IP = "10.0.0.10"
SERVER_PORT = 9999
PLAYER_ID = sys.argv[1] if len(sys.argv) > 1 else "p1"

client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
client.settimeout(5)

last_seq = {}
received_lock = threading.Lock()

client.sendto(json.dumps({
    "type": "register",
    "player_id": PLAYER_ID
}).encode(), (SERVER_IP, SERVER_PORT))

try:
    response, _ = client.recvfrom(4096)
    response = json.loads(response.decode())

    if response.get("type") == "registered":
        print(PLAYER_ID, "registered successfully")
    else:
        print("Registration failed")
        client.close()
        sys.exit(1)

except (socket.timeout, ValueError, UnicodeDecodeError):
    print("Registration failed")
    client.close()
    sys.exit(1)

client.settimeout(None)

def receive_updates():
    while True:
        try:
            data, _ = client.recvfrom(4096)
            message = json.loads(data.decode())

            if message.get("type") != "update":
                continue

            player = message.get("player_id")
            seq = message.get("seq")

            if not isinstance(player, str):
                continue

            if not isinstance(seq, int) or isinstance(seq, bool):
                continue

            with received_lock:
                previous = last_seq.get(player, -1)

                if previous >= 0 and seq > previous + 1:
                    print("Possible packet loss from", player,
                          "missing", seq - previous - 1, "updates")

                if seq <= previous:
                    print("Out-of-order or duplicate update from", player)
                    continue

                last_seq[player] = seq

            print("Received", player, "position:",
                  message.get("x"), message.get("y"),
                  "sequence:", seq)

        except (ValueError, UnicodeDecodeError, OSError):
            print("Error receiving update")

thread = threading.Thread(target=receive_updates, daemon=True)
thread.start()

seq = 0
x = 0
y = 0

try:
    while True:
        x += random.choice([-1, 0, 1])
        y += random.choice([-1, 0, 1])

        update = {
            "type": "update",
            "player_id": PLAYER_ID,
            "x": x,
            "y": y,
            "seq": seq,
            "timestamp": time.time()
        }

        client.sendto(json.dumps(update).encode(),
                      (SERVER_IP, SERVER_PORT))

        seq += 1
        time.sleep(0.1)

except KeyboardInterrupt:
    print("Player disconnected")

finally:
    client.close()
