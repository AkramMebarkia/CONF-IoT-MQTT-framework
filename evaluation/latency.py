import time
import json
from collections import deque
from statistics import mean, stdev

class LatencyTracker:
    def __init__(self, maxlen=5000):
        self.delays = deque(maxlen=maxlen)
        self.timestamps = deque(maxlen=maxlen)
        self.processed_count = 0
        self.error_count = 0
        
        # Track unique messages to avoid double counting
        self.unique_messages = set()  # Store (publisher_name, seq_id) tuples
        self.publisher_message_count = {}  # Track actual publisher messages
        
    def handle_message(self, msg):
        try:
            self.processed_count += 1
            
            # Handle both string and bytes payload
            if isinstance(msg.payload, bytes):
                payload_str = msg.payload.decode('utf-8')
            else:
                payload_str = str(msg.payload)
            
            # Parse JSON with better error handling
            try:
                payload = json.loads(payload_str)
            except json.JSONDecodeError as e:
                print(f"JSON decode error: {e}")
                self.error_count += 1
                return
            
            # Validate payload structure
            if not isinstance(payload, dict):
                print(f"Payload is not dict: {type(payload)}")
                self.error_count += 1
                return
            
            # Extract key information - FIX: Look for publisher_name first, then fall back to name
            ts_sent = payload.get('ts_sent')
            publisher_name = payload.get('publisher_name') or payload.get('original_publisher_name')
            if not publisher_name:
                # The publisher name might be in the nested payload structure
                # Node-RED sends: payload.publisher_name from the original message
                # Let's debug what we're actually getting
                print(f"DEBUG: Payload keys: {list(payload.keys())}")
                print(f"DEBUG: Full payload: {payload}")
            
            seq_id = payload.get('seq_id')
            
            if ts_sent is None:
                print(f"Missing timestamp in payload keys: {list(payload.keys())}")
                self.error_count += 1
                return

            # Track unique publisher messages to get accurate count
            if publisher_name and seq_id:
                message_key = (publisher_name, seq_id)
                if message_key not in self.unique_messages:
                    self.unique_messages.add(message_key)
                    # Count unique messages per publisher
                    if publisher_name not in self.publisher_message_count:
                        self.publisher_message_count[publisher_name] = 0
                    self.publisher_message_count[publisher_name] += 1
                    print(f"DEBUG: Tracked unique message from {publisher_name}, seq {seq_id}")

            try:
                ts_recv = time.time() * 1000  # ms
                ts_sent = float(ts_sent)
                delay = ts_recv - ts_sent
                
                # Sanity check for delay values
                if delay < 0:
                    print(f"Negative delay detected: {delay}ms")
                    self.error_count += 1
                    return
                elif delay > 60000:  # More than 60 seconds seems unrealistic
                    print(f"Suspiciously high delay: {delay}ms")
                
                self.delays.append(delay)
                self.timestamps.append(ts_recv)
                
                # Log every 10th message
                if len(self.delays) % 10 == 0:
                    unique_count = len(self.unique_messages)
                    print(f"Processed {len(self.delays)} delay measurements from {unique_count} unique messages")
                    
            except (ValueError, TypeError) as e:
                print(f"Timestamp conversion error: {e}")
                self.error_count += 1
                return

        except Exception as e:
            print(f"Unexpected processing error: {e}")
            self.error_count += 1

    def _calculate_percentiles(self, delays_list):
        """Calculate percentiles from delay list"""
        if not delays_list:
            return 0.0, 0.0, 0.0
            
        sorted_delays = sorted(delays_list)
        n = len(sorted_delays)
        
        # Calculate percentile indices
        p50_idx = int(n * 0.5)
        p95_idx = int(n * 0.95)
        p99_idx = int(n * 0.99)
        
        # Handle edge cases
        p50 = sorted_delays[min(p50_idx, n-1)]
        p95 = sorted_delays[min(p95_idx, n-1)]
        p99 = sorted_delays[min(p99_idx, n-1)]
        
        return round(p50, 2), round(p95, 2), round(p99, 2)

    def get_stats(self):
        print(f"Getting stats: {len(self.delays)} delays, {self.error_count} errors")
        print(f"Unique publisher messages: {len(self.unique_messages)}")
        print(f"Publisher message breakdown: {self.publisher_message_count}")
        
        if not self.delays:
            return {
                "count": 0,
                "unique_publisher_messages": 0,
                "processed_count": self.processed_count,
                "error_count": self.error_count,
                "avg_delay": 0.0,
                "min_delay": 0.0,
                "max_delay": 0.0,
                "jitter": 0.0,
                "p50": 0.0,
                "p95": 0.0,
                "p99": 0.0,
                "publisher_breakdown": {}
            }

        delays_list = list(self.delays)
        
        try:
            # Calculate percentiles
            p50, p95, p99 = self._calculate_percentiles(delays_list)
            
            stats = {
                "count": len(delays_list),  # Total delay measurements received
                "unique_publisher_messages": len(self.unique_messages),  # Actual unique messages
                "processed_count": self.processed_count,
                "error_count": self.error_count,
                "avg_delay": round(mean(delays_list), 2),
                "min_delay": round(min(delays_list), 2),
                "max_delay": round(max(delays_list), 2),
                "jitter": round(stdev(delays_list), 2) if len(delays_list) >= 2 else 0.0,
                "p50": p50,
                "p95": p95,
                "p99": p99,
                "publisher_breakdown": dict(self.publisher_message_count)
            }
            
            print(f"Final stats: {stats}")
            return stats
            
        except Exception as e:
            print(f"Error calculating stats: {e}")
            return {
                "count": len(delays_list),
                "unique_publisher_messages": len(self.unique_messages),
                "processed_count": self.processed_count,
                "error_count": self.error_count + 1,
                "avg_delay": 0.0,
                "min_delay": 0.0,
                "max_delay": 0.0,
                "jitter": 0.0,
                "p50": 0.0,
                "p95": 0.0,
                "p99": 0.0,
                "publisher_breakdown": dict(self.publisher_message_count)
            }