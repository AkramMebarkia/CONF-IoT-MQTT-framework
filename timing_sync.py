"""
Enhanced Timing Synchronization Module for MQTT Simulation Platform
Implements NTP synchronization and high-precision timing for accurate latency measurements
"""

import time
import threading
import statistics
from datetime import datetime
import ntplib
import socket
from collections import deque
import json


class TimingSynchronizer:
    """
    Handles NTP synchronization and provides synchronized timestamps
    """
    
    def __init__(self, ntp_servers=None, sync_interval=300):
        """
        Initialize timing synchronizer
        
        Args:
            ntp_servers: List of NTP servers to use
            sync_interval: How often to sync with NTP (seconds)
        """
        self.ntp_servers = ntp_servers or [
            'pool.ntp.org',
            'time.cloudflare.com',
            'time.google.com',
            '0.pool.ntp.org'
        ]
        self.sync_interval = sync_interval
        
        # Timing state
        self.time_offset = 0.0  # Offset from system time to NTP time
        self.last_sync_time = 0
        self.sync_quality = 0  # Quality of last sync (0-100)
        self.sync_lock = threading.Lock()
        
        # Sync history for monitoring
        self.sync_history = deque(maxlen=100)
        self.sync_errors = deque(maxlen=50)
        
        # Start background sync
        self.sync_thread = threading.Thread(target=self._sync_worker, daemon=True)
        self.sync_thread.start()
        
        # Initial sync
        self.sync_now()
        
        print(f"[TimingSynchronizer] Initialized with {len(self.ntp_servers)} NTP servers")
        print(f"   Sync interval: {sync_interval}s")
        print(f"   Initial offset: {self.time_offset:.3f}ms")
    
    def sync_now(self):
        """Force immediate NTP synchronization"""
        success = False
        sync_results = []
        
        for server in self.ntp_servers:
            try:
                client = ntplib.NTPClient()
                response = client.request(server, version=3, timeout=2)
                
                # Calculate offset
                offset = response.offset * 1000  # Convert to milliseconds
                sync_results.append({
                    'server': server,
                    'offset': offset,
                    'delay': response.delay * 1000,
                    'precision': response.precision
                })
                
                print(f"[NTP] {server}: offset={offset:.3f}ms, delay={response.delay*1000:.3f}ms")
                success = True
                
            except Exception as e:
                self.sync_errors.append({
                    'server': server,
                    'error': str(e),
                    'timestamp': time.time()
                })
                print(f"[NTP] Failed to sync with {server}: {e}")
                continue
        
        if success and sync_results:
            # Use median offset for robustness
            offsets = [r['offset'] for r in sync_results]
            
            with self.sync_lock:
                self.time_offset = statistics.median(offsets)
                self.last_sync_time = time.time()
                self.sync_quality = min(100, len(sync_results) * 25)  # Quality based on successful syncs
                
                # Store sync history
                self.sync_history.append({
                    'timestamp': self.last_sync_time,
                    'offset': self.time_offset,
                    'quality': self.sync_quality,
                    'servers_used': len(sync_results)
                })
            
            print(f"[NTP] Sync complete: offset={self.time_offset:.3f}ms, quality={self.sync_quality}%")
            return True
        else:
            print("[NTP] WARNING: All NTP servers failed!")
            return False
    
    def _sync_worker(self):
        """Background thread for periodic NTP synchronization"""
        while True:
            try:
                time.sleep(self.sync_interval)
                if time.time() - self.last_sync_time >= self.sync_interval:
                    self.sync_now()
            except Exception as e:
                print(f"[NTP] Sync worker error: {e}")
                time.sleep(60)  # Wait before retrying
    
    def get_ntp_timestamp_ms(self):
        """Get current timestamp synchronized to NTP in milliseconds"""
        with self.sync_lock:
            return (time.time() * 1000) + self.time_offset
    
    def get_monotonic_ms(self):
        """Get monotonic timestamp in milliseconds (for intervals)"""
        return time.monotonic() * 1000
    
    def get_sync_status(self):
        """Get current synchronization status"""
        with self.sync_lock:
            return {
                'last_sync': self.last_sync_time,
                'offset_ms': self.time_offset,
                'quality': self.sync_quality,
                'time_since_sync': time.time() - self.last_sync_time,
                'sync_count': len(self.sync_history),
                'error_count': len(self.sync_errors)
            }


class PrecisionLatencyTracker:
    """
    Enhanced latency tracker with NTP synchronization and precision timing
    """
    
    def __init__(self, warmup_duration=60, timing_sync=None):
        """
        Initialize precision latency tracker
        
        Args:
            warmup_duration: Warmup period in seconds
            timing_sync: TimingSynchronizer instance
        """
        self.timing_sync = timing_sync or TimingSynchronizer()
        self.warmup_duration = warmup_duration
        
        # Warmup state
        self.warmup_start = None
        self.in_warmup = True
        self.warmup_messages = 0
        
        # Measurement data
        self.delays = deque()
        self.raw_delays = deque()
        self.timestamps = deque()
        self.message_metadata = deque()
        
        # Message tracking
        self.processed_count = 0
        self.error_count = 0
        self.clock_skew_events = 0
        self.unique_messages = set()
        self.publisher_stats = {}
        
        # Quality thresholds
        self.MAX_REASONABLE_DELAY = 10000  # 10 seconds
        self.MIN_REASONABLE_DELAY = 0.01   # 0.01 ms
        self.MAX_CLOCK_SKEW = 1000        # 1 second
        
        print(f"[PrecisionLatencyTracker] Initialized with NTP sync")
        print(f"   Warmup duration: {warmup_duration}s")
        print(f"   NTP offset: {self.timing_sync.time_offset:.3f}ms")
    
    def start_warmup(self):
        """Start warmup phase"""
        self.warmup_start = self.timing_sync.get_monotonic_ms()
        self.in_warmup = True
        self.warmup_messages = 0
        print(f"[PrecisionLatencyTracker] Starting {self.warmup_duration}s warmup...")
    
    def check_warmup_complete(self):
        """Check if warmup is complete"""
        if not self.in_warmup:
            return True
            
        if self.warmup_start is None:
            self.start_warmup()
            return False
        
        elapsed = (self.timing_sync.get_monotonic_ms() - self.warmup_start) / 1000
        
        if elapsed >= self.warmup_duration:
            self.in_warmup = False
            print(f"[PrecisionLatencyTracker] Warmup complete! {self.warmup_messages} messages processed")
            
            # Reset counters for actual measurement
            self.delays.clear()
            self.raw_delays.clear()
            self.timestamps.clear()
            self.message_metadata.clear()
            self.processed_count = 0
            self.error_count = 0
            self.unique_messages.clear()
            self.publisher_stats.clear()
            
            return True
        
        return False
    
    def handle_message(self, msg):
        """Process incoming delay measurement message with precision timing"""
        try:
            self.processed_count += 1
            
            # Parse message payload
            try:
                if isinstance(msg.payload, bytes):
                    payload_str = msg.payload.decode('utf-8')
                else:
                    payload_str = str(msg.payload)
                
                payload = json.loads(payload_str)
            except json.JSONDecodeError as e:
                self.error_count += 1
                print(f"[PrecisionLatencyTracker] JSON decode error: {e}")
                return
            
            # Extract required fields
            ts_sent = payload.get('ts_sent')
            publisher_name = payload.get('publisher_name')
            seq_id = payload.get('seq_id')
            
            if ts_sent is None:
                self.error_count += 1
                print(f"[PrecisionLatencyTracker] Missing ts_sent field")
                return
            
            # Handle warmup
            if self.in_warmup:
                self.warmup_messages += 1
                if not self.check_warmup_complete():
                    return
            
            # Get synchronized receive timestamp
            ts_recv = self.timing_sync.get_ntp_timestamp_ms()
            ts_sent = float(ts_sent)
            
            # Calculate latency
            delay = ts_recv - ts_sent
            
            # Validate delay
            if delay < -self.MAX_CLOCK_SKEW:
                self.clock_skew_events += 1
                print(f"[PrecisionLatencyTracker] Large negative delay: {delay:.3f}ms - clock skew detected")
                return
            elif delay < 0:
                # Small negative delays - likely clock jitter, use absolute value
                delay = abs(delay)
                self.clock_skew_events += 1
            
            if delay > self.MAX_REASONABLE_DELAY:
                print(f"[PrecisionLatencyTracker] Unreasonable delay: {delay:.3f}ms - dropping message")
                return
            
            # Ensure minimum delay
            delay = max(delay, self.MIN_REASONABLE_DELAY)
            
            # Track unique messages
            message_key = (publisher_name, seq_id) if publisher_name and seq_id else None
            if message_key:
                if message_key not in self.unique_messages:
                    self.unique_messages.add(message_key)
                    
                    # Update publisher stats
                    if publisher_name not in self.publisher_stats:
                        self.publisher_stats[publisher_name] = {
                            'count': 0,
                            'delays': deque(maxlen=1000),
                            'last_seq': 0
                        }
                    
                    self.publisher_stats[publisher_name]['count'] += 1
                    self.publisher_stats[publisher_name]['delays'].append(delay)
                    self.publisher_stats[publisher_name]['last_seq'] = max(
                        self.publisher_stats[publisher_name]['last_seq'], 
                        seq_id or 0
                    )
            
            # Store measurements
            self.delays.append(delay)
            self.raw_delays.append(delay)
            self.timestamps.append(ts_recv)
            
            # Store metadata
            metadata = {
                'publisher': publisher_name,
                'seq_id': seq_id,
                'ts_sent': ts_sent,
                'ts_recv': ts_recv,
                'ntp_offset': self.timing_sync.time_offset
            }
            self.message_metadata.append(metadata)
            
            # Progress reporting
            if len(self.delays) % 100 == 0:
                recent_avg = statistics.mean(list(self.delays)[-100:])
                ntp_status = self.timing_sync.get_sync_status()
                print(f"[PrecisionLatencyTracker] Progress: {len(self.delays)} messages, "
                      f"recent avg: {recent_avg:.2f}ms, NTP quality: {ntp_status['quality']}%")
                
        except Exception as e:
            self.error_count += 1
            print(f"[PrecisionLatencyTracker] Message handling error: {e}")
            import traceback
            traceback.print_exc()
    
    def get_comprehensive_stats(self):
        """Generate comprehensive statistics with timing analysis"""
        print(f"[PrecisionLatencyTracker] Generating stats from {len(self.delays)} measurements")
        
        if not self.delays:
            return {
                "status": "no_data",
                "timing_sync": self.timing_sync.get_sync_status()
            }
        
        delays_list = list(self.delays)
        
        # Calculate percentiles
        sorted_delays = sorted(delays_list)
        n = len(sorted_delays)
        
        def percentile(data, p):
            k = (n - 1) * p
            f = int(k)
            c = f + 1
            if c >= n:
                return data[f]
            return data[f] + (k - f) * (data[c] - data[f])
        
        # Quality analysis
        validation_issues = []
        
        if len(delays_list) < 100:
            validation_issues.append(f"Insufficient samples: {len(delays_list)} < 100")
        
        avg_delay = statistics.mean(delays_list)
        if avg_delay > 5000:
            validation_issues.append(f"Suspiciously high average: {avg_delay:.2f}ms")
        
        if len(delays_list) >= 2:
            stdev_delay = statistics.stdev(delays_list)
            cv = stdev_delay / avg_delay if avg_delay > 0 else 0
            if cv > 1.5:
                validation_issues.append(f"High variance (CV={cv:.2f})")
        
        # NTP sync quality check
        ntp_status = self.timing_sync.get_sync_status()
        if ntp_status['quality'] < 50:
            validation_issues.append(f"Poor NTP sync quality: {ntp_status['quality']}%")
        
        if ntp_status['time_since_sync'] > 600:  # 10 minutes
            validation_issues.append(f"NTP sync outdated: {ntp_status['time_since_sync']:.0f}s ago")
        
        stats = {
            # Core metrics
            "count": len(delays_list),
            "unique_publisher_messages": len(self.unique_messages),
            "processed_count": self.processed_count,
            
            # Latency metrics
            "avg_delay": round(avg_delay, 3),
            "median_delay": round(percentile(sorted_delays, 0.5), 3),
            "min_delay": round(min(delays_list), 3),
            "max_delay": round(max(delays_list), 3),
            "jitter": round(statistics.stdev(delays_list), 3) if len(delays_list) >= 2 else 0.0,
            
            # Percentiles
            "p25": round(percentile(sorted_delays, 0.25), 3),
            "p50": round(percentile(sorted_delays, 0.50), 3),
            "p75": round(percentile(sorted_delays, 0.75), 3),
            "p90": round(percentile(sorted_delays, 0.90), 3),
            "p95": round(percentile(sorted_delays, 0.95), 3),
            "p99": round(percentile(sorted_delays, 0.99), 3),
            
            # Quality metrics
            "error_count": self.error_count,
            "error_rate": round((self.error_count / max(self.processed_count, 1)) * 100, 2),
            "clock_skew_events": self.clock_skew_events,
            "validation_issues": validation_issues,
            "validation_passed": len(validation_issues) == 0,
            
            # Publisher breakdown
            "publisher_breakdown": {
                name: stats['count'] 
                for name, stats in self.publisher_stats.items()
            },
            
            # Timing sync status
            "timing_sync": ntp_status,
            
            # Configuration
            "warmup_duration": self.warmup_duration,
            "ntp_servers": self.timing_sync.ntp_servers
        }
        
        print(f"\n[PrecisionLatencyTracker] Final Statistics:")
        print(f"   Measurements: {stats['count']}")
        print(f"   Unique messages: {stats['unique_publisher_messages']}")
        print(f"   Average latency: {stats['avg_delay']}ms")
        print(f"   Median latency: {stats['median_delay']}ms")
        print(f"   P95 latency: {stats['p95']}ms")
        print(f"   NTP offset: {ntp_status['offset_ms']:.3f}ms")
        print(f"   Validation: {'PASSED' if stats['validation_passed'] else 'FAILED'}")
        
        return stats


# Integration helper functions

def create_synchronized_timestamp():
    """Create a synchronized timestamp using global timing sync"""
    global _global_timing_sync
    if '_global_timing_sync' not in globals():
        _global_timing_sync = TimingSynchronizer()
    return _global_timing_sync.get_ntp_timestamp_ms()

def get_timing_sync_status():
    """Get global timing sync status"""
    global _global_timing_sync
    if '_global_timing_sync' not in globals():
        return {"error": "Timing sync not initialized"}
    return _global_timing_sync.get_sync_status()