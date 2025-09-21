import time
from collections import deque

class ThroughputTracker:
    def __init__(self, interval_sec=1, window_size=60):
        self.interval = interval_sec
        self.timestamps = deque(maxlen=window_size * 10)
        self.start_time = None
        self.message_count = 0
        
        # NEW: Track unique publisher messages instead of delay messages
        self.unique_publisher_messages = set()  # Store (publisher_name, seq_id) tuples
        self.publisher_timestamps = deque(maxlen=window_size * 10)

    def record_delay_message(self, delay_payload):
        """Record a delay message and extract publisher info"""
        now = time.time()
        if self.start_time is None:
            self.start_time = now
            print(f"Throughput tracking started at {now}")
        
        # Always record the delay message for backward compatibility
        self.timestamps.append(now)
        self.message_count += 1
        
        # NEW: Extract and record unique publisher message
        try:
            publisher_name = delay_payload.get('publisher_name') or delay_payload.get('name', 'unknown')
            seq_id = delay_payload.get('seq_id')
            
            if publisher_name and seq_id:
                message_key = (publisher_name, seq_id)
                if message_key not in self.unique_publisher_messages:
                    self.unique_publisher_messages.add(message_key)
                    self.publisher_timestamps.append(now)
                    
                    if len(self.unique_publisher_messages) % 10 == 0:
                        print(f"Recorded {len(self.unique_publisher_messages)} unique publisher messages")
        except Exception as e:
            print(f"Error extracting publisher info: {e}")

    def record_message(self):
        """Legacy method for backward compatibility"""
        now = time.time()
        if self.start_time is None:
            self.start_time = now
            print(f"Throughput tracking started at {now}")
        
        self.timestamps.append(now)
        self.message_count += 1

    def get_publisher_throughput(self, duration=None):
        """Calculate throughput based on unique publisher messages"""
        if not self.publisher_timestamps:
            print("No unique publisher messages recorded, returning 0.0")
            return 0.0

        if duration:
            # Calculate throughput for the specified duration
            end_time = time.time()
            start_time = end_time - duration
            
            # Filter messages within the duration
            relevant_messages = [t for t in self.publisher_timestamps if t >= start_time]
            throughput = len(relevant_messages) / duration if duration > 0 else 0.0
            
            print(f"Publisher throughput: {len(relevant_messages)} msgs in {duration}s = {throughput:.2f} msg/s")
            return round(throughput, 2)

        # Fallback calculation based on actual time range
        if len(self.publisher_timestamps) >= 2:
            start = self.publisher_timestamps[0]
            end = self.publisher_timestamps[-1]
            time_span = max(end - start, 0.001)
            throughput = len(self.publisher_timestamps) / time_span
        else:
            throughput = 0.0

        print(f"Publisher throughput (range): {len(self.publisher_timestamps)} msgs in {time_span:.3f}s = {throughput:.2f} msg/s")
        return round(throughput, 2)

    def get_throughput(self, duration=None):
        """Calculate throughput based on delay messages (for backward compatibility)"""
        if not self.timestamps:
            print("No delay messages recorded, returning 0.0")
            return 0.0

        if duration:
            end_time = time.time()
            start_time = end_time - duration
            relevant_messages = [t for t in self.timestamps if t >= start_time]
            throughput = len(relevant_messages) / duration if duration > 0 else 0.0
            
            print(f"Delay message throughput: {len(relevant_messages)} msgs in {duration}s = {throughput:.2f} msg/s")
            return round(throughput, 2)

        # Fallback calculation
        if len(self.timestamps) >= 2:
            start = self.timestamps[0]
            end = self.timestamps[-1]
            time_span = max(end - start, 0.001)
            throughput = len(self.timestamps) / time_span
        else:
            throughput = 0.0

        return round(throughput, 2)

    def get_stats(self):
        """Get detailed throughput statistics"""
        delay_stats = {
            "delay_message_count": len(self.timestamps),
            "delay_throughput_mps": self.get_throughput(),
        }
        
        publisher_stats = {
            "unique_publisher_messages": len(self.unique_publisher_messages),
            "publisher_throughput_mps": self.get_publisher_throughput(),
        }
        
        # Calculate duration
        duration = 0.0
        if len(self.publisher_timestamps) >= 2:
            duration = max(self.publisher_timestamps[-1] - self.publisher_timestamps[0], 0.001)
        
        return {
            **delay_stats,
            **publisher_stats,
            "duration": round(duration, 2),
            "start_time": self.publisher_timestamps[0] if self.publisher_timestamps else None,
            "end_time": self.publisher_timestamps[-1] if self.publisher_timestamps else None
        }