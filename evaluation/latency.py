import time
import json
from collections import deque
from statistics import mean, stdev

class LatencyTracker:
    def __init__(self):
        # REMOVE maxlen to allow unlimited messages during evaluation, please refer to the last git commit to see previous version with limited maxlen.
        self.delays = deque()  # No maxlen limit
        self.timestamps = deque()  # No maxlen limit
        self.processed_count = 0
        self.error_count = 0
        
        # Track unique messages to avoid double counting
        self.unique_messages = set()  # Store (publisher_name, seq_id) tuples
        self.publisher_message_count = {}  # Track actual publisher messages
        
    def handle_message(self, msg):
        try:
            self.processed_count += 1
            
            # Parse payload
            if isinstance(msg.payload, bytes):
                payload_str = msg.payload.decode('utf-8')
            else:
                payload_str = str(msg.payload)
            
            try:
                payload = json.loads(payload_str)
            except json.JSONDecodeError as e:
                print(f"JSON decode error: {e}")
                self.error_count += 1
                return
            
            if not isinstance(payload, dict):
                print(f"Payload is not dict: {type(payload)}")
                self.error_count += 1
                return
            
            # ✅ USE THE DELAY ALREADY CALCULATED BY NODE-RED SUBSCRIBER
            delay = payload.get('delay')  # This is the CORRECT value!
            publisher_name = payload.get('publisher_name')
            seq_id = payload.get('seq_id')
            
            if delay is None:
                print(f"Missing delay in payload keys: {list(payload.keys())}")
                self.error_count += 1
                return
            
            if not publisher_name:
                original_topic = payload.get('original_topic', '')
                if original_topic:
                    publisher_name = f"publisher_{original_topic.replace('/', '_')}"
            
            # Track unique publisher messages
            if publisher_name and seq_id is not None:
                message_key = (publisher_name, seq_id)
                if message_key not in self.unique_messages:
                    self.unique_messages.add(message_key)
                    if publisher_name not in self.publisher_message_count:
                        self.publisher_message_count[publisher_name] = 0
                    self.publisher_message_count[publisher_name] += 1
                    
                    if len(self.unique_messages) % 100 == 0:
                        print(f"✓ Unique message #{len(self.unique_messages)}: {publisher_name} seq={seq_id}")
            
            try:
                delay = float(delay)
                
                # Sanity check (but don't need to worry about negative delays anymore!)
                if delay < 0:
                    print(f"WARNING: Negative delay received: {delay}ms (Node-RED clock issue)")
                    self.error_count += 1
                    return
                
                if delay > 60000:  # More than 60 seconds
                    print(f"Suspiciously high delay: {delay}ms from {publisher_name}")
                    # Still record it, might be legitimate under load
                
                self.delays.append(delay)
                self.timestamps.append(time.time() * 1000)  # Just for tracking when we received it
                
                # Log progress every 500 messages
                if len(self.delays) % 500 == 0:
                    unique_count = len(self.unique_messages)
                    avg_delay = mean(list(self.delays)[-500:])
                    print(f"Progress: {len(self.delays)} measurements, {unique_count} unique msgs, "
                        f"Recent avg: {avg_delay:.2f}ms")
                    
            except (ValueError, TypeError) as e:
                print(f"Delay conversion error: {e}")
                self.error_count += 1
                return

        except Exception as e:
            print(f"Unexpected processing error: {e}")
            import traceback
            traceback.print_exc()
            self.error_count += 1

    def _calculate_percentiles(self, delays_list):
        """Calculate percentiles from delay list"""
        if not delays_list or len(delays_list) < 2:
            return 0.0, 0.0, 0.0
            
        sorted_delays = sorted(delays_list)
        n = len(sorted_delays)
        
        def get_percentile(sorted_list, percentile):
            k = (len(sorted_list) - 1) * percentile
            f = int(k)
            c = f + 1
            if c >= len(sorted_list):
                return sorted_list[f]
            d0 = sorted_list[f] * (c - k)
            d1 = sorted_list[c] * (k - f)
            return d0 + d1
        
        p50 = get_percentile(sorted_delays, 0.50)
        p95 = get_percentile(sorted_delays, 0.95)
        p99 = get_percentile(sorted_delays, 0.99)
        
        return round(p50, 2), round(p95, 2), round(p99, 2)

    def get_stats(self):
        print(f"Generating stats: {len(self.delays)} delays, {self.error_count} errors")
        print(f"Unique publisher messages: {len(self.unique_messages)}")
        
        if self.publisher_message_count:
            print(f"Publisher breakdown (top 5):")
            for pub, count in sorted(self.publisher_message_count.items(), key=lambda x: x[1], reverse=True)[:5]:
                print(f"   - {pub}: {count} messages")
        
        if not self.delays:
            return {
                "count": 0,
                "unique_publisher_messages": 0,
                "processed_count": self.processed_count,
                "error_count": self.error_count,
                "error_rate": 0.0,
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
            
            # Calculate error rate properly
            error_rate = (self.error_count / max(self.processed_count, 1)) * 100
            
            stats = {
                "count": len(delays_list),  # Total delay measurements received
                "unique_publisher_messages": len(self.unique_messages),  # Actual unique messages
                "processed_count": self.processed_count,
                "error_count": self.error_count,
                "error_rate": round(error_rate, 2),
                "avg_delay": round(mean(delays_list), 2),
                "min_delay": round(min(delays_list), 2),
                "max_delay": round(max(delays_list), 2),
                "jitter": round(stdev(delays_list), 2) if len(delays_list) >= 2 else 0.0,
                "p50": p50,
                "p95": p95,
                "p99": p99,
                "publisher_breakdown": dict(self.publisher_message_count)
            }
            
            print(f"   Stats generated successfully:")
            print(f"   Total delay measurements: {stats['count']}")
            print(f"   Unique publisher messages: {stats['unique_publisher_messages']}")
            print(f"   Avg delay: {stats['avg_delay']}ms")
            print(f"   P50/P95/P99: {p50}/{p95}/{p99}ms")
            print(f"   Error rate: {stats['error_rate']}%")
            
            return stats
            
        except Exception as e:
            print(f"Error calculating stats: {e}")
            import traceback
            traceback.print_exc()
            
            return {
                "count": len(delays_list),
                "unique_publisher_messages": len(self.unique_messages),
                "processed_count": self.processed_count,
                "error_count": self.error_count + 1,
                "error_rate": 100.0,
                "avg_delay": 0.0,
                "min_delay": 0.0,
                "max_delay": 0.0,
                "jitter": 0.0,
                "p50": 0.0,
                "p95": 0.0,
                "p99": 0.0,
                "publisher_breakdown": dict(self.publisher_message_count)
            }