import socket
import json
import time

HOST = "0.0.0.0"
PORT = 9999

server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
server.bind((HOST, PORT))

players = {}
last_seq = {}

print("Game server running on port", PORT)

while True:
    try:
        data, address = server.recvfrom(4096)
        message = json.loads(data.decode())

        if message.get("type") == "register":
            player_id = message.get("player_id")

            if not isinstance(player_id, str) or not player_id:
                continue

            players[player_id] = address
            last_seq[player_id] = -1

            print(player_id, "registered from", address)

            response = {
                "type": "registered",
                "player_id": player_id
            }

            server.sendto(json.dumps(response).encode(), address)

        elif message.get("type") == "update":
            player_id = message.get("player_id")
            seq = message.get("seq")
            x = message.get("x")
            y = message.get("y")

            if player_id not in players or players[player_id] != address:
                continue

            if not isinstance(seq, int) or isinstance(seq, bool):
                continue

            if not isinstance(x, (int, float)) or isinstance(x, bool):
                continue

            if not isinstance(y, (int, float)) or isinstance(y, bool):
                continue

            if seq <= last_seq.get(player_id, -1):
                continue

            last_seq[player_id] = seq

            message["received_at"] = time.time()
            print(player_id, "position:", x, y, "sequence:", seq)

            packet = json.dumps(message).encode()

            for other_id, other_address in list(players.items()):
                if other_id != player_id:
                    server.sendto(packet, other_address)

    except (ValueError, UnicodeDecodeError, AttributeError, TypeError):
        print("Invalid message received")
    except OSError as error:
        print("Socket error:", error)
