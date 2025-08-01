import time
import json
from collections import deque
from statistics import mean, stdev

class LatencyTracker:
    def __init__(self, maxlen=5000):
        self.delays = deque(maxlen=maxlen)
        self.timestamps = deque(maxlen=maxlen)

    def handle_message(self, msg):
        try:
            payload = json.loads(msg.payload.decode())
            if not isinstance(payload, dict) or 'ts_sent' not in payload:
                return

            ts_recv = time.time() * 1000  # ms
            ts_sent = float(payload['ts_sent'])
            delay = ts_recv - ts_sent
            self.delays.append(delay)
            self.timestamps.append(ts_recv)

        except Exception as e:
            print(f"[Latency Error] {e}")

    def get_stats(self):
        if not self.delays:
            return {}

        return {
            "count": len(self.delays),
            "avg_delay": round(mean(self.delays), 2),
            "min_delay": round(min(self.delays), 2),
            "max_delay": round(max(self.delays), 2),
            "jitter": round(stdev(self.delays), 2) if len(self.delays) >= 2 else 0.0
        }
