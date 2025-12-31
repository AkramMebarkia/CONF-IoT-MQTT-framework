import logging
import time
from collections import deque

logger = logging.getLogger(__name__)
class FixedThroughputTracker:
    def __init__(self, interval_sec=1, window_size=60):
        self.interval = interval_sec
        self.timestamps = deque(maxlen=window_size * 100)
        self.start_time = None
        self.message_count = 0
        
        # Track unique publisher messages
        self.unique_publisher_messages = set()  # Store (publisher_name, seq_id) tuples
        self.publisher_timestamps = deque(maxlen=window_size * 100)
        self.publisher_rates = deque(maxlen=60)  # Store per-second rates

    def record_delay_message(self, delay_payload):
        """Record a delay message and extract publisher info"""
        now = time.time()
        if self.start_time is None:
            self.start_time = now
            logger.debug("Throughput tracking started")
        
        # Always record the delay message timestamp
        self.timestamps.append(now)
        self.message_count += 1
        
        # Extract and record unique publisher message
        try:
            publisher_name = delay_payload.get('publisher_name')
            seq_id = delay_payload.get('seq_id')
            
            # Debug logging for first few messages
            if self.message_count <= 5:
                print(f"[Throughput] Message #{self.message_count}: pub={publisher_name}, seq={seq_id}")
            
            if publisher_name and seq_id is not None:
                message_key = (publisher_name, seq_id)
                if message_key not in self.unique_publisher_messages:
                    self.unique_publisher_messages.add(message_key)
                    self.publisher_timestamps.append(now)
                        
        except Exception as e:
            logger.error("Error extracting publisher info: %s", e)

    def record_message(self):
        """Legacy method - just record timestamp"""
        now = time.time()
        if self.start_time is None:
            self.start_time = now
        
        self.timestamps.append(now)
        self.message_count += 1

    def reset(self):
        """Reset all collected data for fresh start after warm-up"""
        self.timestamps.clear()
        self.start_time = None
        self.message_count = 0
        self.unique_publisher_messages.clear()
        self.publisher_timestamps.clear()
        self.publisher_rates.clear()
        logger.info("Throughput tracker reset")

    def get_publisher_throughput(self, duration=None):
        """Calculate throughput based on unique publisher messages"""
        if not self.publisher_timestamps:
            logger.debug("No unique publisher messages recorded")
            return 0.0

        timestamps_list = list(self.publisher_timestamps)
        
        if duration:
            # Calculate throughput for the specified duration
            end_time = time.time()
            start_time = end_time - duration
            
            # Count messages within the duration
            relevant_messages = [t for t in timestamps_list if t >= start_time]
            if relevant_messages:
                actual_duration = end_time - min(relevant_messages[0], start_time)
                throughput = len(relevant_messages) / actual_duration if actual_duration > 0 else 0.0
            else:
                throughput = 0.0
                
            logger.debug("Publisher: %d msgs in %ds = %.2f msg/s", len(relevant_messages), duration, throughput)
            return round(throughput, 2)

        # Calculate based on actual time range
        if len(timestamps_list) >= 2:
            start = timestamps_list[0]
            end = timestamps_list[-1]
            time_span = max(end - start, 0.001)
            throughput = len(timestamps_list) / time_span
            
            logger.debug("Publisher (full): %d msgs in %.1fs = %.2f msg/s", len(timestamps_list), time_span, throughput)
        else:
            throughput = 0.0

        return round(throughput, 2)

    def get_throughput(self, duration=None):
        """Calculate throughput based on all delay messages"""
        if not self.timestamps:
            logger.debug("No delay messages recorded")
            return 0.0

        timestamps_list = list(self.timestamps)
        
        if duration:
            end_time = time.time()
            start_time = end_time - duration
            relevant_messages = [t for t in timestamps_list if t >= start_time]
            
            if relevant_messages:
                actual_duration = end_time - min(relevant_messages[0], start_time)
                throughput = len(relevant_messages) / actual_duration if actual_duration > 0 else 0.0
            else:
                throughput = 0.0
                
            logger.debug("Delay: %d msgs in %ds = %.2f msg/s", len(relevant_messages), duration, throughput)
            return round(throughput, 2)

        # Calculate based on actual time range
        if len(timestamps_list) >= 2:
            start = timestamps_list[0]
            end = timestamps_list[-1]
            time_span = max(end - start, 0.001)
            throughput = len(timestamps_list) / time_span
        else:
            throughput = 0.0

        return round(throughput, 2)

    def get_stats(self):
        """Get comprehensive throughput statistics"""
        # Basic counts
        delay_count = len(self.timestamps)
        unique_count = len(self.unique_publisher_messages)
        
        # Calculate durations
        duration = 0.0
        if self.publisher_timestamps and len(self.publisher_timestamps) >= 2:
            timestamps_list = list(self.publisher_timestamps)
            duration = timestamps_list[-1] - timestamps_list[0]
        elif self.timestamps and len(self.timestamps) >= 2:
            timestamps_list = list(self.timestamps)
            duration = timestamps_list[-1] - timestamps_list[0]
        
        # Get throughput rates
        delay_throughput = self.get_throughput()
        publisher_throughput = self.get_publisher_throughput()
        
        # Calculate expected vs actual if we have unique messages
        efficiency = 0.0
        if unique_count > 0 and delay_count > 0:
            # This shows the multiplication factor (how many subscribers per publisher message)
            efficiency = round(delay_count / unique_count, 2)
        
        stats = {
            "delay_message_count": delay_count,
            "delay_throughput_mps": delay_throughput,
            "unique_publisher_messages": unique_count,
            "publisher_throughput_mps": publisher_throughput,
            "duration": round(duration, 2),
            "subscriber_multiplication_factor": efficiency,
            "start_time": self.start_time,
            "end_time": time.time() if self.start_time else None
        }
        
        logger.debug("Final stats: %d unique msgs, %d delay msgs, factor=%.2fx, rate=%.2f msg/s",
                    unique_count, delay_count, efficiency, publisher_throughput)
        
        return stats