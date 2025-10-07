"""
Updated Evaluation Controller with NTP Synchronization and Precision Timing
Replaces the original evaluation/controller.py
"""

import threading
import time
import uuid
import json
import paho.mqtt.client as mqtt
from datetime import datetime

from evaluation.availability import AvailabilityMonitor
from evaluation.throughput import ThroughputTracker
from evaluation.stats import StatsAggregator

# Import the new timing synchronization module
from timing_sync import TimingSynchronizer, PrecisionLatencyTracker


class SynchronizedEvaluationController:
    """
    Enhanced evaluation controller with NTP synchronization for accurate latency measurements
    """
    
    def __init__(self, broker_host, broker_port, duration, warmup, 
                 job_id=None, output_dir="results", validate_results=True,
                 ntp_servers=None):
        """
        Initialize synchronized evaluation controller
        
        Args:
            broker_host: MQTT broker hostname
            broker_port: MQTT broker port
            duration: Test duration in seconds (after warmup)
            warmup: Warmup duration in seconds
            job_id: Unique job identifier
            output_dir: Directory for results
            validate_results: Whether to validate results
            ntp_servers: List of NTP servers for synchronization
        """
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.duration = duration
        self.warmup_duration = warmup
        self.validate_results = validate_results
        self.job_id = job_id or uuid.uuid4().hex
        self.output_dir = output_dir
        
        # Initialize timing synchronization FIRST
        print(f"\n[SyncEvaluationController] Initializing NTP synchronization...")
        self.timing_sync = TimingSynchronizer(ntp_servers=ntp_servers)
        
        # Wait for initial sync to complete
        sync_attempts = 0
        max_sync_attempts = 3
        while sync_attempts < max_sync_attempts:
            sync_status = self.timing_sync.get_sync_status()
            if sync_status['quality'] > 25:  # At least one server synced
                print(f"[SyncEvaluationController] NTP sync established (quality: {sync_status['quality']}%)")
                break
            else:
                sync_attempts += 1
                print(f"[SyncEvaluationController] Attempting NTP sync ({sync_attempts}/{max_sync_attempts})")
                self.timing_sync.sync_now()
                time.sleep(2)
        
        # Connection state
        self.connected = False
        self.subscribed = False
        self.message_count = 0
        self.connection_error = None
        self.first_message_time = None
        
        # Expected message tracking
        self.expected_publishers = 0
        self.expected_rate = 0
        self.expected_messages = 0
        
        # Enhanced trackers with NTP sync
        self.latency_tracker = PrecisionLatencyTracker(
            warmup_duration=warmup,
            timing_sync=self.timing_sync
        )
        
        # Keep existing trackers for compatibility
        self.throughput_tracker = ThroughputTracker()
        self.availability_monitor = AvailabilityMonitor()
        
        # MQTT Client with enhanced configuration
        client_id = f"sync_eval_controller_{self.job_id[:8]}"
        self.client = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv311)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.on_disconnect = self.on_disconnect
        self.client.on_subscribe = self.on_subscribe
        
        # Enhanced aggregator
        self.aggregator = StatsAggregator(self.job_id, self.broker_host, output_dir=output_dir)
        
        print(f"\n[SyncEvaluationController] Configuration:")
        print(f"   Broker: {broker_host}:{broker_port}")
        print(f"   Job ID: {self.job_id}")
        print(f"   Warmup: {warmup}s")
        print(f"   Duration: {duration}s")
        print(f"   NTP Servers: {self.timing_sync.ntp_servers}")
        print(f"   Current NTP Offset: {self.timing_sync.time_offset:.3f}ms")
    
    def on_connect(self, client, userdata, flags, rc):
        """Handle MQTT connection with timing info"""
        if rc == 0:
            connect_time = self.timing_sync.get_ntp_timestamp_ms()
            print(f"[SyncEvaluationController] Connected at NTP time: {connect_time:.3f}")
            self.connected = True
            self.connection_error = None
            
            # Subscribe to delay statistics topic
            result, mid = client.subscribe("sim/stats/delay", qos=1)
            print(f"[SyncEvaluationController] Subscribe request sent (mid={mid})")
        else:
            error_msg = f"Connection failed with code {rc}"
            print(f"[SyncEvaluationController] {error_msg}")
            self.connected = False
            self.connection_error = error_msg
    
    def on_subscribe(self, client, userdata, mid, granted_qos):
        """Handle subscription confirmation"""
        self.subscribed = True
        print(f"[SyncEvaluationController] Successfully subscribed (mid={mid}, qos={granted_qos})")
    
    def on_disconnect(self, client, userdata, rc):
        """Handle disconnection"""
        disconnect_time = self.timing_sync.get_ntp_timestamp_ms()
        print(f"[SyncEvaluationController] Disconnected at NTP time: {disconnect_time:.3f} (code: {rc})")
        self.connected = False
        self.subscribed = False
        if rc != 0:
            print(f"[SyncEvaluationController] WARNING: Unexpected disconnection!")
    
    def on_message(self, client, userdata, msg):
        """Process incoming delay measurement messages with synchronized timing"""
        try:
            self.message_count += 1
            
            # Record precise message receive time
            msg_recv_time = self.timing_sync.get_ntp_timestamp_ms()
            
            # Track first message time for warmup
            if self.first_message_time is None:
                self.first_message_time = msg_recv_time
                print(f"[SyncEvaluationController] First message at NTP time: {msg_recv_time:.3f}")
                self.latency_tracker.start_warmup()
            
            # Process through precision latency tracker
            self.latency_tracker.handle_message(msg)
            
            # Also process through throughput tracker for compatibility
            try:
                payload_str = msg.payload.decode() if isinstance(msg.payload, bytes) else str(msg.payload)
                payload = json.loads(payload_str)
                self.throughput_tracker.record_delay_message(payload)
            except Exception:
                self.throughput_tracker.record_message()
            
            # Enhanced progress reporting
            if self.message_count % 100 == 0:
                elapsed = (msg_recv_time - self.first_message_time) / 1000 if self.first_message_time else 0
                rate = self.message_count / max(elapsed, 1)
                
                # Get current NTP sync status
                ntp_status = self.timing_sync.get_sync_status()
                
                if self.latency_tracker.in_warmup:
                    phase = f"WARMUP ({self.latency_tracker.warmup_messages} msgs)"
                else:
                    phase = f"MEASUREMENT ({len(self.latency_tracker.delays)} msgs)"
                
                print(f"[SyncEvaluationController] {phase} | Total: {self.message_count} | "
                      f"Rate: {rate:.1f} msg/s | NTP: {ntp_status['quality']}%")
                
        except Exception as e:
            print(f"[SyncEvaluationController] Message handling error: {e}")
            import traceback
            traceback.print_exc()
    
    def set_expected_values(self, publisher_count, publish_rate):
        """Set expected values for validation"""
        self.expected_publishers = publisher_count
        self.expected_rate = publish_rate
        self.expected_messages = publisher_count * publish_rate * self.duration
        print(f"[SyncEvaluationController] Expected: {publisher_count} pubs × {publish_rate} msg/s × {self.duration}s = {self.expected_messages} msgs")
    
    def validate_timing_quality(self):
        """Validate timing synchronization quality"""
        ntp_status = self.timing_sync.get_sync_status()
        issues = []
        
        if ntp_status['quality'] < 50:
            issues.append(f"Poor NTP sync quality: {ntp_status['quality']}%")
        
        if abs(ntp_status['offset_ms']) > 100:
            issues.append(f"Large NTP offset: {ntp_status['offset_ms']:.1f}ms")
        
        if ntp_status['time_since_sync'] > 600:  # 10 minutes
            issues.append(f"Stale NTP sync: {ntp_status['time_since_sync']:.0f}s ago")
        
        if ntp_status['error_count'] > 5:
            issues.append(f"Many NTP errors: {ntp_status['error_count']}")
        
        return issues
    
    def run(self):
        """Run synchronized evaluation with enhanced timing accuracy"""
        print(f"\n[SyncEvaluationController] Starting synchronized evaluation...")
        print(f"   Phase 1: NTP sync verification & MQTT connection")
        print(f"   Phase 2: {self.warmup_duration}s synchronized warmup")
        print(f"   Phase 3: {self.duration}s precision measurement")
        print(f"   Phase 4: Timing quality analysis & validation")
        
        # Phase 1: Verify NTP sync and connect to MQTT
        evaluation_start = self.timing_sync.get_ntp_timestamp_ms()
        
        # Check NTP sync quality before starting
        timing_issues = self.validate_timing_quality()
        if timing_issues:
            print(f"\n⚠️ Timing Quality Warnings:")
            for issue in timing_issues:
                print(f"   - {issue}")
            print("Proceeding with evaluation but results may be less accurate...\n")
        
        # Start availability monitoring
        monitor_thread = threading.Thread(
            target=self.availability_monitor.monitor,
            args=(self.broker_host, self.broker_port, self.warmup_duration + self.duration),
            daemon=True
        )
        monitor_thread.start()
        
        try:
            print(f"\n[Phase 1] Establishing MQTT connection...")
            self.client.loop_start()
            
            # Connect with timeout
            connect_result = self.client.connect(self.broker_host, self.broker_port, keepalive=60)
            if connect_result != 0:
                raise Exception(f"MQTT connect failed: {connect_result}")
            
            # Wait for connection and subscription
            connection_timeout = 30
            for i in range(connection_timeout):
                if self.connected and self.subscribed:
                    break
                time.sleep(1)
                if i % 5 == 0:
                    print(f"[Phase 1] Waiting for MQTT setup... ({i}/{connection_timeout}s)")
            
            if not self.connected:
                raise Exception("Failed to establish MQTT connection")
            
            print(f"[Phase 1] MQTT connection established!")
            
        except Exception as e:
            print(f"[Phase 1] FAILED: {e}")
            self.client.loop_stop()
            return {"error": str(e), "job_id": self.job_id}
        
        # Phase 2 & 3: Synchronized measurement phases
        print(f"\n[Phase 2] Waiting for first message to begin synchronized measurement...")
        
        # Wait for first message with timeout
        wait_start = self.timing_sync.get_monotonic_ms()
        while self.first_message_time is None and (self.timing_sync.get_monotonic_ms() - wait_start) < 30000:
            time.sleep(1)
            elapsed = int((self.timing_sync.get_monotonic_ms() - wait_start) / 1000)
            if elapsed % 5 == 0 and elapsed > 0:
                print(f"[Phase 2] Waiting for first message... ({elapsed}/30s)")
        
        if self.first_message_time is None:
            print("[Phase 2] WARNING: No messages received, starting measurement anyway")
            self.first_message_time = self.timing_sync.get_ntp_timestamp_ms()
            self.latency_tracker.start_warmup()
        
        # Calculate phase end times using NTP synchronized time
        warmup_end_time = self.first_message_time + (self.warmup_duration * 1000)
        measurement_end_time = warmup_end_time + (self.duration * 1000)
        
        # Monitor progress with precise timing
        last_report_time = self.timing_sync.get_ntp_timestamp_ms()
        last_ntp_check = last_report_time
        
        while self.timing_sync.get_ntp_timestamp_ms() < measurement_end_time:
            current_time = self.timing_sync.get_ntp_timestamp_ms()
            
            # Check NTP sync health every 60 seconds
            if current_time - last_ntp_check >= 60000:
                ntp_status = self.timing_sync.get_sync_status()
                if ntp_status['quality'] < 25:
                    print(f"[WARNING] NTP sync quality degraded: {ntp_status['quality']}%")
                last_ntp_check = current_time
            
            # Report progress every 5 seconds
            if current_time - last_report_time >= 5000:
                elapsed = (current_time - self.first_message_time) / 1000
                
                if current_time < warmup_end_time:
                    # During warmup
                    warmup_remaining = (warmup_end_time - current_time) / 1000
                    print(f"[Phase 2] WARMUP - {elapsed:.0f}s elapsed, {warmup_remaining:.0f}s remaining")
                    print(f"         Warmup messages: {self.latency_tracker.warmup_messages}")
                    print(f"         NTP offset: {self.timing_sync.time_offset:.3f}ms")
                else:
                    # During measurement
                    if self.latency_tracker.in_warmup:
                        print(f"\n[Phase 3] Transitioning to MEASUREMENT phase...")
                        # Force end warmup if not already done
                        self.latency_tracker.in_warmup = False
                        self.latency_tracker.delays.clear()
                        self.latency_tracker.raw_delays.clear()
                    
                    measurement_elapsed = (current_time - warmup_end_time) / 1000
                    measurement_remaining = (measurement_end_time - current_time) / 1000
                    
                    print(f"[Phase 3] MEASUREMENT - {measurement_elapsed:.0f}s elapsed, {measurement_remaining:.0f}s remaining")
                    print(f"         Valid measurements: {len(self.latency_tracker.delays)}")
                    
                    if len(self.latency_tracker.delays) > 0:
                        recent_delays = list(self.latency_tracker.delays)[-100:]
                        recent_avg = sum(recent_delays) / len(recent_delays)
                        print(f"         Recent avg latency: {recent_avg:.2f}ms")
                        print(f"         Measurement rate: {len(self.latency_tracker.delays) / max(measurement_elapsed, 1):.1f} msg/s")
                
                last_report_time = current_time
            
            time.sleep(1)
        
        # Phase 4: Enhanced analysis with timing validation
        print(f"\n[Phase 4] Analyzing synchronized measurement results...")
        evaluation_end = self.timing_sync.get_ntp_timestamp_ms()
        total_evaluation_time = (evaluation_end - evaluation_start) / 1000
        
        # Stop MQTT and monitoring
        self.client.loop_stop()
        self.client.disconnect()
        monitor_thread.join(timeout=5)
        
        # Get comprehensive statistics from precision tracker
        latency_stats = self.latency_tracker.get_comprehensive_stats()
        throughput_stats = self.throughput_tracker.get_stats()
        availability_stats = self.availability_monitor.get_stats()
        
        # Enhanced validation with timing quality
        validation_issues = latency_stats.get('validation_issues', [])
        
        # Additional timing-specific validations
        final_timing_issues = self.validate_timing_quality()
        if final_timing_issues:
            validation_issues.extend([f"Timing: {issue}" for issue in final_timing_issues])
        
        # Check measurement duration accuracy
        expected_measurement_duration = self.duration * 1000
        if len(self.latency_tracker.timestamps) >= 2:
            actual_duration = max(self.latency_tracker.timestamps) - min(self.latency_tracker.timestamps)
            duration_error = abs(actual_duration - expected_measurement_duration) / expected_measurement_duration
            if duration_error > 0.05:  # 5% tolerance
                validation_issues.append(f"Measurement duration error: {duration_error*100:.1f}%")
        
        # Final timing sync status
        final_ntp_status = self.timing_sync.get_sync_status()
        
        # Print comprehensive summary
        print(f"\n[Phase 4] Synchronized Evaluation Summary:")
        print(f"   Total evaluation time: {total_evaluation_time:.1f}s")
        print(f"   NTP synchronization:")
        print(f"      Final offset: {final_ntp_status['offset_ms']:.3f}ms")
        print(f"      Sync quality: {final_ntp_status['quality']}%")
        print(f"      Servers used: {len(self.timing_sync.ntp_servers)}")
        print(f"   Message statistics:")
        print(f"      Warmup messages: {self.latency_tracker.warmup_messages}")
        print(f"      Valid measurements: {len(self.latency_tracker.delays)}")
        print(f"      Unique publisher messages: {len(self.latency_tracker.unique_messages)}")
        print(f"      Clock skew events: {self.latency_tracker.clock_skew_events}")
        print(f"   Precision latency metrics:")
        print(f"      Average: {latency_stats.get('avg_delay', 0):.3f}ms")
        print(f"      Median: {latency_stats.get('median_delay', 0):.3f}ms")
        print(f"      P95: {latency_stats.get('p95', 0):.3f}ms")
        print(f"      P99: {latency_stats.get('p99', 0):.3f}ms")
        print(f"      Jitter (σ): {latency_stats.get('jitter', 0):.3f}ms")
        
        if validation_issues:
            print(f"\n⚠️  Validation Issues Detected:")
            for issue in validation_issues:
                print(f"   - {issue}")
            latency_stats['validation_issues'] = validation_issues
            latency_stats['validation_passed'] = False
        else:
            print(f"\n✓ All validation checks passed - high accuracy measurement")
            latency_stats['validation_passed'] = True
        
        # Add timing metadata to results
        latency_stats['evaluation_duration_s'] = total_evaluation_time
        latency_stats['ntp_final_status'] = final_ntp_status
        
        # Aggregate all statistics
        self.aggregator.add_module_stats("latency", latency_stats)
        self.aggregator.add_module_stats("throughput", throughput_stats)
        self.aggregator.add_module_stats("availability", availability_stats)
        
        # Save results with timing metadata
        result = self.aggregator.get_summary()
        result["summary"]["timing_synchronized"] = True
        result["summary"]["ntp_servers_used"] = self.timing_sync.ntp_servers
        
        print(f"\n[SyncEvaluationController] Evaluation complete!")
        print(f"   Results saved to: {result['saved_to']}")
        print(f"   Measurement accuracy: {'HIGH' if not validation_issues else 'DEGRADED'}")
        
        return result["summary"]