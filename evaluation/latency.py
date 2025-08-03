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
            
            # Parse JSON
            payload = json.loads(payload_str)
            
            if not isinstance(payload, dict):
                print(f"⚠️  [Latency] Payload is not dict: {type(payload)}")
                self.error_count += 1
                return
                
            if 'ts_sent' not in payload:
                print(f"⚠️  [Latency] Missing ts_sent in payload keys: {list(payload.keys())}")
                self.error_count += 1
                return

            ts_recv = time.time() * 1000  # ms
            ts_sent = float(payload['ts_sent'])
            delay = ts_recv - ts_sent
            
            self.delays.append(delay)
            self.timestamps.append(ts_recv)
            
            if len(self.delays) % 10 == 0:  # Log every 10th message
                print(f"📊 [Latency] Processed {len(self.delays)} delays, latest: {delay:.2f}ms")

        except json.JSONDecodeError as e:
            print(f"❌ [Latency] JSON decode error: {e}")
            print(f"   Raw payload: {msg.payload}")
            self.error_count += 1
        except Exception as e:
            print(f"❌ [Latency] Processing error: {e}")
            self.error_count += 1

    def get_stats(self):
        print(f"📊 [Latency] Getting stats: {len(self.delays)} delays, {self.error_count} errors")
        
        if not self.delays:
            return {
                "count": 0,
                "processed_count": self.processed_count,
                "error_count": self.error_count
            }

        return {
            "count": len(self.delays),
            "processed_count": self.processed_count,
            "error_count": self.error_count,
            "avg_delay": round(mean(self.delays), 2),
            "min_delay": round(min(self.delays), 2),
            "max_delay": round(max(self.delays), 2),
            "jitter": round(stdev(self.delays), 2) if len(self.delays) >= 2 else 0.0
        }