import logging
import threading
import time
import uuid

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from evaluation.latency import EnhancedLatencyTracker
from evaluation.throughput import FixedThroughputTracker
from evaluation.availability import AvailabilityMonitor
from evaluation.stats import StatsAggregator

logger = logging.getLogger(__name__)


class EvaluationController:
    def __init__(self, broker_host, broker_port, duration=60, job_id=None, output_dir="results", delay_queue=None):
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.duration = duration
        self.job_id = job_id or uuid.uuid4().hex
        self.output_dir = output_dir
        self.connected = False
        self.message_count = 0
        self.connection_error = None
        self.delay_queue = delay_queue

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
            
            connect_result = self.client.connect(self.broker_host, self.broker_port, keepalive=60)
            if connect_result != 0:
                raise Exception(f"Connect returned error code: {connect_result}")
            
            connection_timeout = 10
            for i in range(connection_timeout):
                if self.connected:
                    break
                if self.connection_error:
                    raise Exception(f"Connection error: {self.connection_error}")
                logger.info("Waiting for connection... (%d/%d)", i+1, connection_timeout)
                time.sleep(1)
            
            if not self.connected:
                raise Exception("Failed to establish MQTT connection within timeout")
                
            logger.info("Ready to collect data for %d seconds", self.duration)
            time.sleep(5)
            
        except Exception as e:
            logger.error("MQTT setup failed: %s", e)
            self.client.loop_stop()
            return {"error": str(e), "job_id": self.job_id}

        start_time = time.time()
        last_count = 0
        last_report_time = start_time
        no_message_warnings = 0
        
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

        self.client.loop_stop()
        self.client.disconnect()
        monitor_thread.join(timeout=3)

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

        result = self.aggregator.get_summary()
        logger.info("Evaluation complete. Summary saved to %s", result['saved_to'])
        return result["summary"]