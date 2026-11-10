"""
common.py - shared protocol constants and the sequence-number bookkeeping.

Protocol (JSON over UDP). It EXTENDS the D1 protocol (type / player_id / seq /
timestamp) - D1 messages are still understood.

  client -> server
    {"type":"register", "player_id":"p1", "session":123}
    {"type":"update",   "player_id":"p1", "session":123, "seq":0, "x":10, "y":20,
                        "timestamp":1700000000.1,                # client send time
                        "rtt":3.2, "jitter":0.4,                 # last values measured by the client
                        "down_recv":120, "down_lost":2}          # what the client received from others
    {"type":"bye",      "player_id":"p1"}
  server -> client
    {"type":"registered","player_id":"p1"}
    {"type":"ack",      "player_id":"p1", "session":123, "seq":0, "timestamp":<echo>}   # RTT measurement
    {"type":"update", ...same fields as above..., "received_at":...}                    # other players' positions (D1 behaviour)
    {"type":"error",    "msg":"..."}
"""
import re

GAME_PORT = 9999
BG_PORT = 10000
SERVER_IP = "10.0.0.10"
MAX_TRACKED_GAP = 1000
ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,16}$")


class PlayerTracker:
    """Sequence-number bookkeeping for ONE sender (sequence numbers start at 0).

    Invariant inside one session:   received + lost == highest + 1

    on_packet() returns:
      "new"        seq is higher than anything seen (skipped numbers are counted as lost)
      "late"       seq was counted lost before, now arrived out of order -> received once
      "duplicate"  seq already seen -> ignored
    A different session id (sender restarted) resets all counters.
    """

    def __init__(self):
        self.session = None
        self.restarts = 0
        self.reset(None)

    def reset(self, session):
        if self.session is not None and session is not None:
            self.restarts += 1
        self.session = session
        self.highest = -1
        self.missing = set()
        self.received = 0
        self.lost = 0
        self.duplicates = 0
        self.out_of_order = 0

    def on_packet(self, session, seq, join_midstream=False):
        """join_midstream=True: this observer started listening while the sender was already
        running, so updates sent BEFORE the first one we saw are not counted as lost."""
        if session != self.session:
            self.reset(session)
        if join_midstream and self.highest == -1 and self.received == 0:
            self.highest = seq - 1
        if seq > self.highest:
            gap = seq - self.highest - 1
            if gap > 0:
                self.lost += gap
                if gap <= MAX_TRACKED_GAP:
                    self.missing.update(range(self.highest + 1, seq))
            self.highest = seq
            self.received += 1
            return "new"
        if seq in self.missing:
            self.missing.discard(seq)
            self.lost -= 1
            self.received += 1
            self.out_of_order += 1
            return "late"
        self.duplicates += 1
        return "duplicate"

    def loss_pct(self):
        total = self.highest + 1
        if total <= 0:
            return 0.0
        return 100.0 * self.lost / total


def clean_id(value):
    if isinstance(value, str) and ID_PATTERN.match(value):
        return value
    return None


def to_float(value, default=0.0):
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):
        return default
    return number


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)
