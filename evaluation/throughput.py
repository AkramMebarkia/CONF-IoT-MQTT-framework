import time
from collections import deque

class ThroughputTracker:
    def __init__(self, interval_sec=1, window_size=60):
        self.interval = interval_sec
        self.timestamps = deque(maxlen=window_size * 10)  # 10x to handle bursty traffic
        self.start_time = None
        self.message_count = 0

    def record_message(self):
        now = time.time()
        if self.start_time is None:
            self.start_time = now
            print(f"📈 [Throughput] Started tracking at {now}")
        
        self.timestamps.append(now)
        self.message_count += 1
        
        # FIX: Add periodic logging
        if self.message_count % 10 == 0:
            print(f"📈 [Throughput] Recorded {self.message_count} messages")

    def get_throughput(self, duration=None):
        """Calculate throughput in messages per second"""
        if not self.timestamps:
            print("📈 [Throughput] No messages recorded, returning 0.0")
            return 0.0

        if duration:
            # Calculate throughput for the specified duration
            end_time = time.time()
            start_time = end_time - duration
            
            # Filter messages within the duration
            relevant_messages = [t for t in self.timestamps if t >= start_time]
            throughput = len(relevant_messages) / duration if duration > 0 else 0.0
            
            print(f"📈 [Throughput] Duration-based: {len(relevant_messages)} msgs in {duration}s = {throughput:.2f} msg/s")
            return round(throughput, 2)

        # FIX: Fallback calculation based on actual time range
        if len(self.timestamps) >= 2:
            start = self.timestamps[0]
            end = self.timestamps[-1]
            time_span = max(end - start, 0.001)  # Avoid division by zero
            throughput = len(self.timestamps) / time_span
        else:
            # Single message or no time span
            throughput = 0.0

        print(f"📈 [Throughput] Range-based: {len(self.timestamps)} msgs in {time_span:.3f}s = {throughput:.2f} msg/s")
        return round(throughput, 2)

    def get_stats(self):
        """Get detailed throughput statistics"""
        if not self.timestamps:
            return {
                "message_count": 0,
                "throughput_mps": 0.0,
                "duration": 0.0,
                "start_time": None,
                "end_time": None
            }

        start = self.timestamps[0]
        end = self.timestamps[-1]
        duration = max(end - start, 0.001)
        throughput = len(self.timestamps) / duration

        return {
            "message_count": len(self.timestamps),
            "throughput_mps": round(throughput, 2),
            "duration": round(duration, 2),
            "start_time": start,
            "end_time": end
        }