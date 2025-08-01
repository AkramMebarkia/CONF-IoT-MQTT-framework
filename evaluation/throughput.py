import time
from collections import deque

class ThroughputTracker:
    def __init__(self, interval_sec=1, window_size=60):
        self.interval = interval_sec
        self.timestamps = deque(maxlen=window_size * 10)  # 10x to handle bursty traffic

    def record_message(self):
        now = time.time()
        self.timestamps.append(now)

    def get_throughput(self, duration=None):
        if not self.timestamps:
            return 0.0

        if duration:
            start_time = time.time() - duration
            relevant = [t for t in self.timestamps if t >= start_time]
            return round(len(relevant) / duration, 2)

        # fallback: measure based on total range
        start = self.timestamps[0]
        end = self.timestamps[-1]
        duration = max(end - start, 1e-3)
        return round(len(self.timestamps) / duration, 2)