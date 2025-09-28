import threading
import time
import uuid
import json
import paho.mqtt.client as mqtt
from datetime import datetime

from evaluation.latency import LatencyTracker
from evaluation.throughput import ThroughputTracker
from evaluation.availability import AvailabilityMonitor
from evaluation.stats import StatsAggregator


class EvaluationController:
    def __init__(self, broker_host, broker_port, duration, warmup, 
                 job_id=None, output_dir="results", validate_results=True):
        """
        Initialize evaluation controller with proper warmup and validation
        
        Args:
            broker_host: MQTT broker hostname
            broker_port: MQTT broker port
            duration: Test duration in seconds (after warmup)
            warmup: Warmup duration in seconds
            job_id: Unique job identifier
            output_dir: Directory for results
            validate_results: Whether to validate results
        """
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.duration = duration
        self.warmup_duration = warmup
        self.validate_results = validate_results
        self.job_id = job_id or uuid.uuid4().hex
        self.output_dir = output_dir
        
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
        
        # Trackers with proper configuration
        self.latency_tracker = LatencyTracker(
            warmup_duration=warmup,
            outlier_percentile=1  # Remove top/bottom 1%
        )
        self.throughput_tracker = ThroughputTracker()
        self.availability_monitor = AvailabilityMonitor()
        
        # MQTT Client
        client_id = f"eval_controller_{self.job_id[:8]}"
        self.client = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv311)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.on_disconnect = self.on_disconnect
        self.client.on_subscribe = self.on_subscribe
        
        # Aggregator
        self.aggregator = StatsAggregator(self.job_id, self.broker_host, output_dir=output_dir)
        
        print(f"\n[EvaluationController] Configuration:")
        print(f"   Broker: {broker_host}:{broker_port}")
        print(f"   Job ID: {self.job_id}")
        print(f"   Warmup: {warmup}s")
        print(f"   Duration: {duration}s")
        print(f"   Total time: {warmup + duration}s")
        print(f"   Validation: {'Enabled' if validate_results else 'Disabled'}")

    def on_connect(self, client, userdata, flags, rc):
        """Handle MQTT connection"""
        if rc == 0:
            print(f"[EvaluationController] Connected to {self.broker_host}:{self.broker_port}")
            self.connected = True
            self.connection_error = None
            
            # Subscribe to delay statistics topic
            result, mid = client.subscribe("sim/stats/delay", qos=1)
            print(f"[EvaluationController] Subscribe request sent (mid={mid})")
        else:
            error_msg = f"Connection failed with code {rc}"
            print(f"[EvaluationController] {error_msg}")
            self.connected = False
            self.connection_error = error_msg

    def on_subscribe(self, client, userdata, mid, granted_qos):
        """Handle subscription confirmation"""
        self.subscribed = True
        print(f"[EvaluationController] Successfully subscribed (mid={mid}, qos={granted_qos})")

    def on_disconnect(self, client, userdata, rc):
        """Handle disconnection"""
        print(f"[EvaluationController] Disconnected (code: {rc})")
        self.connected = False
        self.subscribed = False
        if rc != 0:
            print(f"[EvaluationController] WARNING: Unexpected disconnection!")

    def on_message(self, client, userdata, msg):
        """Process incoming delay measurement messages"""
        try:
            self.message_count += 1
            
            # Track first message time
            if self.first_message_time is None:
                self.first_message_time = time.time()
                print(f"[EvaluationController] First message received! Starting warmup...")
                # Start warmup phase
                self.latency_tracker.start_warmup()
            
            # Process message through latency tracker (handles warmup internally)
            self.latency_tracker.handle_message(msg)
            
            # Also track in throughput (for backward compatibility)
            try:
                payload_str = msg.payload.decode() if isinstance(msg.payload, bytes) else str(msg.payload)
                payload = json.loads(payload_str)
                self.throughput_tracker.record_delay_message(payload)
            except Exception as e:
                # Fallback
                self.throughput_tracker.record_message()
            
            # Progress reporting every 100 messages
            if self.message_count % 100 == 0:
                elapsed = time.time() - self.first_message_time if self.first_message_time else 0
                rate = self.message_count / max(elapsed, 1)
                
                if self.latency_tracker.in_warmup:
                    phase = f"WARMUP ({self.latency_tracker.warmup_messages} msgs)"
                else:
                    phase = f"MEASUREMENT ({len(self.latency_tracker.delays)} msgs)"
                
                print(f"[EvaluationController] {phase} | Total: {self.message_count} | Rate: {rate:.1f} msg/s")
                
        except Exception as e:
            print(f"[EvaluationController] Message handling error: {e}")
            import traceback
            traceback.print_exc()

    def set_expected_values(self, publisher_count, publish_rate):
        """Set expected values for validation"""
        self.expected_publishers = publisher_count
        self.expected_rate = publish_rate
        self.expected_messages = publisher_count * publish_rate * self.duration
        print(f"[EvaluationController] Expected: {publisher_count} publishers × {publish_rate} msg/s × {self.duration}s = {self.expected_messages} messages")

    def validate_message_counts(self, actual_count):
        """Validate if actual message count is within acceptable range"""
        if self.expected_messages == 0:
            return True, "No expected values set"
        
        tolerance = 0.10  # 10% tolerance
        deviation = abs(actual_count - self.expected_messages) / self.expected_messages
        
        if deviation <= tolerance:
            return True, f"Within tolerance ({deviation:.1%})"
        else:
            return False, f"Outside tolerance ({deviation:.1%} > {tolerance:.1%})"

    def run(self):
        """Run evaluation with proper phases"""
        print(f"\n[EvaluationController] Starting evaluation run...")
        print(f"   Phase 1: Connection & Subscription")
        print(f"   Phase 2: {self.warmup_duration}s warmup")
        print(f"   Phase 3: {self.duration}s measurement")
        print(f"   Phase 4: Data analysis & validation")
        
        # Phase 1: Connection setup
        phase_start = time.time()
        
        # Start broker availability monitoring
        monitor_thread = threading.Thread(
            target=self.availability_monitor.monitor,
            args=(self.broker_host, self.broker_port, self.warmup_duration + self.duration),
            daemon=True
        )
        monitor_thread.start()
        
        try:
            print(f"\n[Phase 1] Connecting to MQTT broker...")
            self.client.loop_start()
            
            # Connect with timeout
            connect_result = self.client.connect(self.broker_host, self.broker_port, keepalive=60)
            if connect_result != 0:
                raise Exception(f"Connect returned error code: {connect_result}")
            
            # Wait for connection
            connection_timeout = 30
            for i in range(connection_timeout):
                if self.connected and self.subscribed:
                    break
                time.sleep(1)
                if i % 5 == 0:
                    print(f"[Phase 1] Waiting for connection... ({i}/{connection_timeout}s)")
            
            if not self.connected:
                raise Exception("Failed to establish MQTT connection")
            
            if not self.subscribed:
                print(f"[Phase 1] WARNING: Subscription not confirmed, continuing anyway...")
            
            phase_duration = time.time() - phase_start
            print(f"[Phase 1] Complete! Connected in {phase_duration:.1f}s")
            
        except Exception as e:
            print(f"[Phase 1] FAILED: {e}")
            self.client.loop_stop()
            return {"error": str(e), "job_id": self.job_id}
        
        # Phase 2 & 3: Wait for warmup + measurement
        print(f"\n[Phase 2] Waiting for first message to start warmup...")
        
        # Wait for first message (max 30 seconds)
        wait_start = time.time()
        while self.first_message_time is None and time.time() - wait_start < 30:
            time.sleep(1)
            elapsed = int(time.time() - wait_start)
            if elapsed % 5 == 0:
                print(f"[Phase 2] Waiting for first message... ({elapsed}/30s)")
        
        if self.first_message_time is None:
            print("[Phase 2] WARNING: No messages received after 30s, starting timer anyway")
            self.first_message_time = time.time()
            self.latency_tracker.start_warmup()
        
        # Track phases
        warmup_end_time = self.first_message_time + self.warmup_duration
        measurement_end_time = warmup_end_time + self.duration
        
        # Monitor progress
        last_report_time = time.time()
        
        while time.time() < measurement_end_time:
            current_time = time.time()
            
            # Report progress every 5 seconds
            if current_time - last_report_time >= 5:
                elapsed = current_time - self.first_message_time
                
                if current_time < warmup_end_time:
                    # During warmup
                    warmup_remaining = warmup_end_time - current_time
                    print(f"[Phase 2] WARMUP - {elapsed:.0f}s elapsed, {warmup_remaining:.0f}s remaining")
                    print(f"         Warmup messages: {self.latency_tracker.warmup_messages}")
                else:
                    # During measurement
                    if self.latency_tracker.in_warmup:
                        # Transition to measurement
                        print(f"\n[Phase 3] Transitioning to MEASUREMENT phase...")
                        self.latency_tracker.end_warmup()
                    
                    measurement_elapsed = current_time - warmup_end_time
                    measurement_remaining = measurement_end_time - current_time
                    
                    print(f"[Phase 3] MEASUREMENT - {measurement_elapsed:.0f}s elapsed, {measurement_remaining:.0f}s remaining")
                    print(f"         Valid messages: {len(self.latency_tracker.delays)}")
                    print(f"         Current rate: {len(self.latency_tracker.delays) / max(measurement_elapsed, 1):.1f} msg/s")
                
                last_report_time = current_time
            
            time.sleep(1)
        
        # Phase 4: Analysis and validation
        print(f"\n[Phase 4] Analyzing results...")
        
        # Stop MQTT and monitoring
        self.client.loop_stop()
        self.client.disconnect()
        monitor_thread.join(timeout=3)
        
        # Get statistics
        latency_stats = self.latency_tracker.get_stats()
        throughput_stats = self.throughput_tracker.get_stats()
        availability_stats = self.availability_monitor.get_stats()
        
        # Validation checks
        validation_issues = []
        
        # Check for sufficient samples
        unique_messages = latency_stats.get('unique_publisher_messages', 0)
        if unique_messages < 100:
            validation_issues.append(f"Insufficient samples: {unique_messages} < 100")
        
        # Check for unrealistic latencies
        avg_delay = latency_stats.get('avg_delay', 0)
        if avg_delay > 5000:  # 5 seconds
            validation_issues.append(f"Unrealistic average latency: {avg_delay}ms")
        
        # Check for high variance
        if avg_delay > 0:
            cv = latency_stats.get('jitter', 0) / avg_delay
            if cv > 2.0:
                validation_issues.append(f"High coefficient of variation: {cv:.2f}")
        
        # Validate message counts if expected values were set
        if self.expected_messages > 0:
            valid, message = self.validate_message_counts(unique_messages)
            if not valid:
                validation_issues.append(f"Message count validation failed: {message}")
        
        # Print summary
        print(f"\n[Phase 4] Evaluation Summary:")
        print(f"   Total runtime: {time.time() - phase_start:.1f}s")
        print(f"   Warmup messages discarded: {self.latency_tracker.warmup_messages}")
        print(f"   Valid measurements: {len(self.latency_tracker.delays)}")
        print(f"   Unique publisher messages: {unique_messages}")
        print(f"   Average latency: {avg_delay:.2f}ms")
        print(f"   Median latency: {latency_stats.get('p50', 0):.2f}ms")
        print(f"   95th percentile: {latency_stats.get('p95', 0):.2f}ms")
        print(f"   99th percentile: {latency_stats.get('p99', 0):.2f}ms")
        
        if validation_issues:
            print(f"\n⚠️ Validation Issues Detected:")
            for issue in validation_issues:
                print(f"   - {issue}")
            latency_stats['validation_issues'] = validation_issues
        else:
            print(f"\n✓ All validation checks passed")
        
        # Add validation status to stats
        latency_stats['validation_passed'] = len(validation_issues) == 0
        
        # Aggregate statistics
        self.aggregator.add_module_stats("latency", latency_stats)
        self.aggregator.add_module_stats("throughput", throughput_stats)
        self.aggregator.add_module_stats("availability", availability_stats)
        
        # Save and return results
        result = self.aggregator.get_summary()
        print(f"\n[EvaluationController] Complete! Results saved to {result['saved_to']}")
        
        return result["summary"]