import logging
import threading
import time
import uuid

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from evaluation.latency import EnhancedLatencyTracker
from evaluation.throughput import FixedThroughputTracker
from evaluation.availability import AvailabilityMonitor
from evaluation.crash_injector import CrashInjector
from evaluation.stats import StatsAggregator

logger = logging.getLogger(__name__)


class EvaluationController:
    def __init__(self, broker_host, broker_port, duration=60, job_id=None, output_dir="results", delay_queue=None, warmup_seconds=10, aggregated_stats_ref=None, aggregated_stats_lock=None, crash_schedule=None, container_name=None):
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.duration = duration
        self.warmup_seconds = warmup_seconds
        self.job_id = job_id or uuid.uuid4().hex
        self.output_dir = output_dir
        self.connected = False
        self.message_count = 0
        self.connection_error = None
        self.delay_queue = delay_queue
        self.aggregated_stats_ref = aggregated_stats_ref  # Reference to global aggregated stats
        self.aggregated_stats_lock = aggregated_stats_lock # Lock for safe reset

        # Crash injection
        self.crash_schedule = crash_schedule or []
        self.container_name = container_name
        self.crash_injector = None

        self.latency_tracker = EnhancedLatencyTracker()
        self.throughput_tracker = FixedThroughputTracker()
        self.availability_monitor = AvailabilityMonitor()

        client_id = f"eval_controller_{self.job_id[:8]}"
        self.client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=mqtt.MQTTv311
        )
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.on_disconnect = self.on_disconnect
        self.client.on_subscribe = self.on_subscribe
        # Enable automatic reconnection for crash recovery
        self.client.reconnect_delay_set(min_delay=1, max_delay=10)

        self.aggregator = StatsAggregator(self.job_id, self.broker_host, output_dir=output_dir)

    def on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            logger.info("Connected to %s:%d", self.broker_host, self.broker_port)
            self.connected = True
            self.connection_error = None
        else:
            error_msg = f"Connection failed with code {reason_code}"
            logger.error("Controller connection failed: %s", error_msg)
            self.connected = False
            self.connection_error = error_msg

    def on_subscribe(self, client, userdata, mid, reason_codes, properties=None):
        logger.info("Successfully subscribed (mid=%d)", mid)

    def on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        logger.info("Controller disconnected (code: %s)", reason_code)
        self.connected = False

    def on_message(self, client, userdata, msg):
        try:
            self.message_count += 1
            logger.debug("Message #%d received on %s", self.message_count, msg.topic)
        except Exception as e:
            logger.error("Message handling error: %s", e)

    def run(self):
        logger.info("Starting evaluation for %ds", self.duration)
        logger.info("Broker: %s:%d, Job ID: %s", self.broker_host, self.broker_port, self.job_id)
        
        monitor_thread = threading.Thread(
            target=self.availability_monitor.monitor,
            args=(self.broker_host, self.broker_port, self.duration),
            daemon=True
        )
        monitor_thread.start()

        try:
            logger.info("Connecting to MQTT broker at %s:%d", self.broker_host, self.broker_port)
            
            self.client.loop_start()
            
            # Connect to broker
            self.client.connect(self.broker_host, self.broker_port, 60)

            # Subscribe to topics
            self.client.subscribe("benchmark/#", qos=0)
            
            # Wait for connection
            start_wait = time.time()
            while not self.connected and time.time() - start_wait < 10:
                time.sleep(1)
            
            if not self.connected:
                raise Exception("Failed to establish MQTT connection within timeout")
                
            logger.info("Ready to collect data for %d seconds (after %ds warm-up)", self.duration, self.warmup_seconds)
            
            # Warm-up period - let things stabilize
            if self.warmup_seconds > 0:
                logger.info("Warm-up period: %d seconds (data will be discarded)", self.warmup_seconds)
                time.sleep(self.warmup_seconds)
                
                # Clear any data collected during warm-up
                if self.delay_queue:
                    discarded = 0
                    while True:
                        try:
                            self.delay_queue.popleft()
                            discarded += 1
                        except IndexError:
                            break
                    if discarded > 0:
                        logger.info("Discarded %d warm-up samples", discarded)
                
                # Reset trackers for fresh start
                self.latency_tracker.reset()
                self.throughput_tracker.reset()
                self.message_count = 0
                
                # Critical: Reset aggregated stats (Node-RED) from warmup period
                if self.aggregated_stats_ref is not None and self.aggregated_stats_lock is not None:
                    with self.aggregated_stats_lock:
                        self.aggregated_stats_ref['count'] = 0
                        self.aggregated_stats_ref['sum'] = 0.0
                        self.aggregated_stats_ref['sum_sq'] = 0.0
                        self.aggregated_stats_ref['min'] = float('inf')
                        self.aggregated_stats_ref['max'] = float('-inf')
                        self.aggregated_stats_ref['windows_received'] = 0
                    logger.info("Reset aggregated stats for measurement phase")
                
                logger.info("Warm-up complete, starting measurement")
            
        except Exception as e:
            logger.error("MQTT setup failed: %s", e)
            self.client.loop_stop()
            return {"error": str(e), "job_id": self.job_id}

        start_time = time.time()
        last_count = 0
        last_report_time = start_time
        no_message_warnings = 0

        # Launch crash injector if configured
        if self.crash_schedule and self.container_name:
            self.crash_injector = CrashInjector(
                container_name=self.container_name,
                crash_schedule=self.crash_schedule,
                broker_port=self.broker_port,
            )
            self.crash_injector.start(measurement_start_time=start_time)
            logger.info("Crash injector launched with %d scheduled crash(es)",
                       len(self.crash_schedule))
        
        while time.time() - start_time < self.duration:
            current_time = time.time()
            current_count = self.message_count
            elapsed = int(current_time - start_time)

            drained = 0
            if self.delay_queue is not None:
                while True:
                    try:
                        rec = self.delay_queue.popleft()
                        self.latency_tracker.handle_message(rec)
                        self.throughput_tracker.record_delay_message(rec)
                        drained += 1
                    except IndexError:
                        break
            self.message_count += drained
            
            if current_time - last_report_time >= 5:
                rate = (current_count - last_count) / (current_time - last_report_time)
                remaining = self.duration - elapsed
                total_delays = len(self.latency_tracker.delays)
                
                logger.info("Progress: %ds/%ds | Samples: %d | Rate: %.1f msg/s | Remaining: %ds",
                           elapsed, self.duration, total_delays, rate, remaining)
                
                if current_count == 0 and elapsed >= 10:
                    no_message_warnings += 1
                    logger.warning("No messages received after %d seconds!", elapsed)
                
                last_count = current_count
                last_report_time = current_time
                
            time.sleep(1)

        logger.info("Data collection finished. Total messages: %d, Latency samples: %d",
                   self.message_count, len(self.latency_tracker.delays))

        # Stop crash injector and collect events
        crash_summary = None
        if self.crash_injector:
            self.crash_injector.stop()
            crash_summary = self.crash_injector.get_summary()
            # Feed crash events to availability monitor for correlation
            self.availability_monitor.set_injected_crash_events(
                crash_summary.get("crash_events", [])
            )
            logger.info("Crash injector finished: %d/%d successful recoveries",
                       crash_summary.get("successful_recoveries", 0),
                       crash_summary.get("total_crashes_executed", 0))

        self.client.loop_stop()
        self.client.disconnect()
        monitor_thread.join(timeout=3)

        # Ingest aggregated stats from Node-RED if available
        if self.aggregated_stats_ref is not None:
            try:
                # Read the current aggregated stats
                agg_data = {
                    'count': self.aggregated_stats_ref.get('count', 0),
                    'sum': self.aggregated_stats_ref.get('sum', 0),
                    'sum_sq': self.aggregated_stats_ref.get('sum_sq', 0),
                    'min': self.aggregated_stats_ref.get('min', float('inf')),
                    'max': self.aggregated_stats_ref.get('max', float('-inf'))
                }
                if agg_data['count'] > 0:
                    self.latency_tracker.add_aggregated_stats(agg_data)
                    # Also add to throughput tracker
                    self.throughput_tracker.add_aggregated_stats(agg_data['count'], self.duration)
                    logger.info("Ingested %d aggregated samples from Node-RED", agg_data['count'])
            except Exception as e:
                logger.error("Failed to ingest aggregated stats: %s", e)

        latency_stats = self.latency_tracker.get_stats()
        throughput_stats = self.throughput_tracker.get_stats()
        availability_stats = self.availability_monitor.get_stats()
        
        logger.info("Final Statistics:")
        logger.info("  Messages processed: %d", self.message_count)
        logger.info("  Unique publisher messages: %d", latency_stats.get('unique_publisher_messages', 0))
        logger.info("  Latency: avg=%.2fms, p50=%.2fms, p95=%.2fms",
                   latency_stats.get('avg_delay', 0),
                   latency_stats.get('p50', 0),
                   latency_stats.get('p95', 0))

        if self.message_count == 0:
            logger.error("No messages were received during evaluation!")
            latency_stats['diagnostic_info'] = {
                'no_messages_received': True,
                'evaluation_duration': self.duration,
                'broker_host': self.broker_host,
                'broker_port': self.broker_port
            }

        self.aggregator.add_module_stats("latency", latency_stats)
        self.aggregator.add_module_stats("throughput", throughput_stats)
        self.aggregator.add_module_stats("availability", availability_stats)
        if crash_summary:
            self.aggregator.add_module_stats("crash_recovery", crash_summary)

        result = self.aggregator.get_summary()
        logger.info("Evaluation complete. Summary saved to %s", result['saved_to'])
        return result["summary"]