import time
import json
from collections import deque
from statistics import mean, stdev, quantiles
from datetime import datetime
import threading

class LatencyTracker:
    def __init__(self, warmup_duration=60, outlier_percentile=1):
        """
        Initialize latency tracker with proper time synchronization
        
        Args:
            warmup_duration: Seconds to wait for system stabilization
            outlier_percentile: Percentage of outliers to remove (top and bottom)
        """
        # Timing synchronization
        self.reference_time_real = None  # Will be set on first message
        self.reference_time_mono = None  # Monotonic reference
        self.time_sync_lock = threading.Lock()
        
        # Warmup phase
        self.warmup_duration = warmup_duration
        self.warmup_start = None
        self.in_warmup = True
        self.warmup_messages = 0
        
        # Main data storage (no limit during measurement)
        self.delays = deque()
        self.raw_delays = deque()  # Keep raw data for analysis
        self.timestamps = deque()
        
        # Message tracking
        self.processed_count = 0
        self.error_count = 0
        self.dropped_count = 0
        self.unique_messages = set()
        self.publisher_message_count = {}
        
        # Outlier handling
        self.outlier_percentile = outlier_percentile
        self.outliers_removed = 0
        
        # Clock skew detection
        self.clock_skew_events = 0
        self.max_clock_skew = 0
        
        # Validation thresholds
        self.MAX_REASONABLE_LATENCY = 5000  # 5 seconds
        self.MIN_REASONABLE_LATENCY = 0.1   # 0.1 ms
        
        print(f"[LatencyTracker] Initialized with {warmup_duration}s warmup, {outlier_percentile}% outlier removal")
    
    def synchronize_time(self, sender_timestamp_ms):
        """
        Establish time reference point for consistent latency calculation
        """
        with self.time_sync_lock:
            if self.reference_time_real is None:
                # First message - establish reference
                self.reference_time_real = time.time()
                self.reference_time_mono = time.monotonic()
                print(f"[LatencyTracker] Time reference established at {datetime.now().isoformat()}")
                return self.reference_time_real * 1000
            
            # Calculate expected time based on monotonic clock
            mono_elapsed = time.monotonic() - self.reference_time_mono
            expected_real_time = (self.reference_time_real + mono_elapsed) * 1000
            
            return expected_real_time
    
    def start_warmup(self):
        """Start the warmup phase"""
        self.warmup_start = time.monotonic()
        self.in_warmup = True
        self.warmup_messages = 0
        print(f"[LatencyTracker] Starting {self.warmup_duration}s warmup phase...")
    
    def check_warmup_complete(self):
        """Check if warmup phase is complete"""
        if not self.in_warmup:
            return True
            
        if self.warmup_start is None:
            self.start_warmup()
            return False
        
        elapsed = time.monotonic() - self.warmup_start
        if elapsed >= self.warmup_duration:
            self.in_warmup = False
            print(f"[LatencyTracker] Warmup complete! Processed {self.warmup_messages} warmup messages")
            print(f"[LatencyTracker] Starting actual measurement phase...")
            
            # Reset counters for actual measurement
            self.delays.clear()
            self.raw_delays.clear()
            self.timestamps.clear()
            self.processed_count = 0
            self.error_count = 0
            self.unique_messages.clear()
            self.publisher_message_count.clear()
            
            return True
        
        # Still in warmup
        remaining = self.warmup_duration - elapsed
        if int(elapsed) % 10 == 0 and elapsed > 0:  # Report every 10 seconds
            print(f"[LatencyTracker] Warmup: {elapsed:.0f}s elapsed, {remaining:.0f}s remaining, {self.warmup_messages} messages")
        
        return False
    
    def handle_message(self, msg):
        """Process a delay measurement message with validation"""
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
                self.error_count += 1
                return
            
            # Extract fields
            ts_sent = payload.get('ts_sent')
            publisher_name = payload.get('publisher_name')
            seq_id = payload.get('seq_id')
            
            if ts_sent is None:
                self.error_count += 1
                return
            
            # Handle warmup phase
            if self.in_warmup:
                self.warmup_messages += 1
                if not self.check_warmup_complete():
                    return  # Still in warmup, don't record
            
            # Synchronize time and calculate latency
            current_time_ms = self.synchronize_time(ts_sent)
            ts_sent = float(ts_sent)
            
            # Calculate latency using synchronized time
            delay = current_time_ms - ts_sent
            
            # Validate latency
            if delay < -100:  # Allow small negative values for clock jitter
                self.clock_skew_events += 1
                self.max_clock_skew = max(self.max_clock_skew, abs(delay))
                
                # For small skews, use absolute value
                if abs(delay) < 1000:  # Less than 1 second
                    delay = abs(delay)
                    if self.clock_skew_events % 10 == 0:
                        print(f"[LatencyTracker] Clock skew detected (#{self.clock_skew_events}): {delay:.2f}ms")
                else:
                    # Large skew - drop message
                    self.dropped_count += 1
                    print(f"[LatencyTracker] Dropped message due to large clock skew: {delay:.2f}ms")
                    return
            
            # Check for unreasonable delays
            if delay > self.MAX_REASONABLE_LATENCY:
                self.dropped_count += 1
                if self.dropped_count <= 10:  # Only log first 10
                    print(f"[LatencyTracker] Dropped unreasonable delay: {delay:.2f}ms from {publisher_name}")
                return
            
            if delay < self.MIN_REASONABLE_LATENCY:
                delay = self.MIN_REASONABLE_LATENCY  # Floor at minimum
            
            # Track unique messages
            if publisher_name and seq_id is not None:
                message_key = (publisher_name, seq_id)
                if message_key not in self.unique_messages:
                    self.unique_messages.add(message_key)
                    
                    if publisher_name not in self.publisher_message_count:
                        self.publisher_message_count[publisher_name] = 0
                    self.publisher_message_count[publisher_name] += 1
                    
                    # Progress reporting
                    if len(self.unique_messages) % 100 == 0:
                        avg_recent = mean(list(self.delays)[-100:]) if len(self.delays) >= 100 else delay
                        print(f"[LatencyTracker] Progress: {len(self.unique_messages)} unique messages, "
                              f"recent avg: {avg_recent:.2f}ms")
            
            # Store delay
            self.delays.append(delay)
            self.raw_delays.append(delay)  # Keep raw for analysis
            self.timestamps.append(current_time_ms)
            
        except Exception as e:
            self.error_count += 1
            print(f"[LatencyTracker] Error processing message: {e}")
    
    def remove_outliers(self, data_list):
        """Remove statistical outliers from data"""
        if len(data_list) < 10:
            return data_list  # Need minimum samples
        
        sorted_data = sorted(data_list)
        n = len(sorted_data)
        
        # Calculate indices for outlier removal
        lower_cut = int(n * (self.outlier_percentile / 100))
        upper_cut = int(n * (1 - self.outlier_percentile / 100))
        
        # Remove outliers
        cleaned = sorted_data[lower_cut:upper_cut]
        self.outliers_removed = n - len(cleaned)
        
        if self.outliers_removed > 0:
            print(f"[LatencyTracker] Removed {self.outliers_removed} outliers ({self.outlier_percentile}% each tail)")
        
        return cleaned
    
    def calculate_percentiles(self, delays_list):
        """Calculate percentiles with proper method"""
        if not delays_list or len(delays_list) < 2:
            return 0.0, 0.0, 0.0, 0.0, 0.0
        
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
        
        p25 = get_percentile(sorted_delays, 0.25)
        p50 = get_percentile(sorted_delays, 0.50)
        p75 = get_percentile(sorted_delays, 0.75)
        p95 = get_percentile(sorted_delays, 0.95)
        p99 = get_percentile(sorted_delays, 0.99)
        
        return (round(p25, 2), round(p50, 2), round(p75, 2), 
                round(p95, 2), round(p99, 2))
    
    def validate_results(self):
        """Validate if results are statistically sound"""
        issues = []
        
        if len(self.delays) < 100:
            issues.append(f"Insufficient samples: {len(self.delays)} < 100")
        
        if len(self.delays) > 0:
            avg_delay = mean(self.delays)
            
            if avg_delay > self.MAX_REASONABLE_LATENCY / 2:
                issues.append(f"Suspiciously high average latency: {avg_delay:.2f}ms")
            
            if len(self.delays) >= 2:
                cv = stdev(self.delays) / avg_delay if avg_delay > 0 else 0
                if cv > 2.0:
                    issues.append(f"Extremely high variance (CV={cv:.2f})")
        
        if self.clock_skew_events > len(self.delays) * 0.1:
            issues.append(f"Too many clock skew events: {self.clock_skew_events}")
        
        if self.dropped_count > len(self.delays) * 0.1:
            issues.append(f"Too many dropped messages: {self.dropped_count}")
        
        return issues
    
    def get_stats(self):
        """Generate comprehensive statistics with validation"""
        print(f"[LatencyTracker] Generating statistics from {len(self.delays)} measurements")
        
        # Validate results
        validation_issues = self.validate_results()
        if validation_issues:
            print(f"[LatencyTracker] WARNING - Validation issues detected:")
            for issue in validation_issues:
                print(f"   - {issue}")
        
        if not self.delays:
            return {
                "count": 0,
                "status": "no_data",
                "validation_issues": validation_issues,
                "warmup_duration": self.warmup_duration
            }
        
        # Remove outliers for cleaner statistics
        cleaned_delays = self.remove_outliers(list(self.delays))
        
        if not cleaned_delays:
            return {
                "count": len(self.delays),
                "status": "all_outliers",
                "validation_issues": validation_issues
            }
        
        # Calculate percentiles
        p25, p50, p75, p95, p99 = self.calculate_percentiles(cleaned_delays)
        
        # Calculate IQR for additional outlier detection
        iqr = p75 - p25
        
        # Error rate calculation
        total_attempts = self.processed_count + self.error_count
        error_rate = (self.error_count / max(total_attempts, 1)) * 100
        
        stats = {
            # Core metrics
            "count": len(self.delays),
            "count_cleaned": len(cleaned_delays),
            "unique_publisher_messages": len(self.unique_messages),
            
            # Timing metrics
            "avg_delay": round(mean(cleaned_delays), 2),
            "median_delay": round(p50, 2),
            "min_delay": round(min(cleaned_delays), 2),
            "max_delay": round(max(cleaned_delays), 2),
            "jitter": round(stdev(cleaned_delays), 2) if len(cleaned_delays) >= 2 else 0.0,
            
            # Percentiles
            "p25": p25,
            "p50": p50,
            "p75": p75,
            "p95": p95,
            "p99": p99,
            "iqr": round(iqr, 2),
            
            # Quality metrics
            "processed_count": self.processed_count,
            "error_count": self.error_count,
            "dropped_count": self.dropped_count,
            "error_rate": round(error_rate, 2),
            "outliers_removed": self.outliers_removed,
            
            # Clock synchronization
            "clock_skew_events": self.clock_skew_events,
            "max_clock_skew_ms": round(self.max_clock_skew, 2),
            
            # Validation
            "validation_issues": validation_issues,
            "validation_passed": len(validation_issues) == 0,
            
            # Configuration
            "warmup_duration": self.warmup_duration,
            "outlier_percentile": self.outlier_percentile,
            
            # Publisher breakdown
            "publisher_breakdown": dict(self.publisher_message_count)
        }
        
        # Print summary
        print(f"\n[LatencyTracker] Final Statistics:")
        print(f"   Total measurements: {stats['count']}")
        print(f"   After outlier removal: {stats['count_cleaned']}")
        print(f"   Unique publisher messages: {stats['unique_publisher_messages']}")
        print(f"   Average latency: {stats['avg_delay']}ms")
        print(f"   Median latency: {stats['median_delay']}ms")
        print(f"   P95 latency: {stats['p95']}ms")
        print(f"   Validation: {'PASSED' if stats['validation_passed'] else 'FAILED'}")
        
        return stats