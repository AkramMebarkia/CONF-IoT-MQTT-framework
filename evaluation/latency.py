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
                print(f"❌ [Latency] JSON decode error: {e}")
                print(f"   Raw payload (first 200 chars): {payload_str[:200]}")
                self.error_count += 1
                return
            
            # Validate payload structure
            if not isinstance(payload, dict):
                print(f"⚠️  [Latency] Payload is not dict: {type(payload)}")
                print(f"   Payload content: {payload}")
                self.error_count += 1
                return
            
            # FIX: More robust timestamp extraction
            ts_sent = None
            if 'ts_sent' in payload:
                ts_sent = payload['ts_sent']
            elif 'timestamp' in payload:  # Fallback
                ts_sent = payload['timestamp']
            
            if ts_sent is None:
                print(f"⚠️  [Latency] Missing timestamp in payload keys: {list(payload.keys())}")
                self.error_count += 1
                return

            try:
                ts_recv = time.time() * 1000  # ms
                ts_sent = float(ts_sent)
                delay = ts_recv - ts_sent
                
                # FIX: Sanity check for delay values
                if delay < 0:
                    print(f"⚠️  [Latency] Negative delay detected: {delay}ms (sent: {ts_sent}, recv: {ts_recv})")
                    self.error_count += 1
                    return
                elif delay > 60000:  # More than 60 seconds seems unrealistic
                    print(f"⚠️  [Latency] Suspiciously high delay: {delay}ms")
                    # Don't return here, but log it
                
                self.delays.append(delay)
                self.timestamps.append(ts_recv)
                
                # FIX: More frequent logging for debugging
                if len(self.delays) % 5 == 0:  # Log every 5th message
                    print(f"📊 [Latency] Processed {len(self.delays)} delays, latest: {delay:.2f}ms (from {payload.get('name', 'unknown')})")
                    
            except (ValueError, TypeError) as e:
                print(f"❌ [Latency] Timestamp conversion error: {e}")
                print(f"   ts_sent value: {ts_sent} (type: {type(ts_sent)})")
                self.error_count += 1
                return

        except Exception as e:
            print(f"❌ [Latency] Unexpected processing error: {e}")
            print(f"   Message topic: {msg.topic}")
            print(f"   Payload type: {type(msg.payload)}")
            self.error_count += 1

    def get_stats(self):
        print(f"📊 [Latency] Getting stats: {len(self.delays)} delays, {self.error_count} errors")
        
        if not self.delays:
            return {
                "count": 0,
                "processed_count": self.processed_count,
                "error_count": self.error_count,
                "avg_delay": 0.0,
                "min_delay": 0.0,
                "max_delay": 0.0,
                "jitter": 0.0
            }

        delays_list = list(self.delays)  # Convert deque to list for calculations
        
        try:
            stats = {
                "count": len(delays_list),
                "processed_count": self.processed_count,
                "error_count": self.error_count,
                "avg_delay": round(mean(delays_list), 2),
                "min_delay": round(min(delays_list), 2),
                "max_delay": round(max(delays_list), 2),
                "jitter": round(stdev(delays_list), 2) if len(delays_list) >= 2 else 0.0
            }
            
            print(f"📊 [Latency] Final stats: {stats}")
            return stats
            
        except Exception as e:
            print(f"❌ [Latency] Error calculating stats: {e}")
            return {
                "count": len(delays_list),
                "processed_count": self.processed_count,
                "error_count": self.error_count + 1,
                "avg_delay": 0.0,
                "min_delay": 0.0,
                "max_delay": 0.0,
                "jitter": 0.0
            }