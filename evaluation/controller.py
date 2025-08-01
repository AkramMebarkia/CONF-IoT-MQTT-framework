import threading
import time
import uuid
import paho.mqtt.client as mqtt

from evaluation.latency import LatencyTracker
from evaluation.throughput import ThroughputTracker
from evaluation.availability import AvailabilityMonitor
from evaluation.stats import StatsAggregator


class EvaluationController:
    def __init__(self, broker_host, broker_port, duration=60, job_id=None, output_dir="results"):
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.duration = duration
        self.job_id = job_id or uuid.uuid4().hex
        self.output_dir = output_dir

        # Trackers
        self.latency_tracker = LatencyTracker()
        self.throughput_tracker = ThroughputTracker()
        self.availability_monitor = AvailabilityMonitor()

        # MQTT
        self.client = mqtt.Client()
        self.client.on_message = self.on_message

        # Aggregator
        self.aggregator = StatsAggregator(self.job_id, self.broker_host, output_dir=output_dir)

    def on_message(self, client, userdata, msg):
        if msg.topic == "sim/stats/delay":
            self.latency_tracker.handle_message(msg)
            self.throughput_tracker.record_message()

    def run(self):
        # Start broker availability monitoring in parallel
        monitor_thread = threading.Thread(
            target=self.availability_monitor.monitor,
            args=(self.broker_host, self.broker_port, self.duration),
            daemon=True
        )
        monitor_thread.start()

        # Setup and start MQTT
        try:
            self.client.connect(self.broker_host, self.broker_port, keepalive=60)
            self.client.subscribe("sim/stats/delay")
            self.client.loop_start()
        except Exception as e:
            print(f"[Controller Error] MQTT connection failed: {e}")
            return {"error": str(e), "job_id": self.job_id}

        # Collect data for duration
        time.sleep(self.duration)

        # Shutdown MQTT and monitoring
        self.client.loop_stop()
        self.client.disconnect()
        monitor_thread.join(timeout=3)

        # Gather stats
        self.aggregator.add_module_stats("latency", self.latency_tracker.get_stats())
        self.aggregator.add_module_stats("throughput", {
            "throughput_mps": self.throughput_tracker.get_throughput(self.duration)
        })
        self.aggregator.add_module_stats("availability", self.availability_monitor.get_stats())

        # Save + return summary
        result = self.aggregator.get_summary()
        print(f"[{self.job_id}] Evaluation complete. Summary saved to {result['saved_to']}")
        return result["summary"]
