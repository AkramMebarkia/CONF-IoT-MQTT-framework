import threading
import time
import uuid
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

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
        self.connected = False
        self.message_count = 0

        # Trackers
        self.latency_tracker = LatencyTracker()
        self.throughput_tracker = ThroughputTracker()
        self.availability_monitor = AvailabilityMonitor()

        # MQTT - Fixed client initialization
        self.client = mqtt.Client(CallbackAPIVersion.VERSION2)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.on_disconnect = self.on_disconnect

        # Aggregator
        self.aggregator = StatsAggregator(self.job_id, self.broker_host, output_dir=output_dir)

    def on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            print(f"✅ [Controller] Connected to {self.broker_host}:{self.broker_port}")
            self.connected = True
            client.subscribe("sim/stats/delay")
            print("✅ [Controller] Subscribed to sim/stats/delay")
        else:
            print(f"❌ [Controller] Connection failed with code {rc}")
            self.connected = False

    def on_disconnect(self, client, userdata, flags, rc, properties=None):
        print(f"🔌 [Controller] Disconnected (code: {rc})")
        self.connected = False

    def on_message(self, client, userdata, msg):
        try:
            self.message_count += 1
            print(f"📨 [Controller] Message #{self.message_count} received on {msg.topic}")
            
            if msg.topic == "sim/stats/delay":
                self.latency_tracker.handle_message(msg)
                self.throughput_tracker.record_message()
                
                # Debug: Show what we received
                try:
                    payload_str = msg.payload.decode() if isinstance(msg.payload, bytes) else str(msg.payload)
                    payload = json.loads(payload_str)
                    print(f"   📊 Delay: {payload.get('delay', 'N/A')}ms, From: {payload.get('name', 'unknown')}")
                except:
                    print(f"   ⚠️  Raw payload: {msg.payload}")
                    
        except Exception as e:
            print(f"❌ [Controller] Message handling error: {e}")

    def run(self):
        print(f"🚀 [Controller] Starting evaluation for {self.duration}s")
        
        # Start broker availability monitoring in parallel
        monitor_thread = threading.Thread(
            target=self.availability_monitor.monitor,
            args=(self.broker_host, self.broker_port, self.duration),
            daemon=True
        )
        monitor_thread.start()

        # Setup and start MQTT with proper error handling
        try:
            print(f"🔗 [Controller] Connecting to MQTT broker at {self.broker_host}:{self.broker_port}")
            
            connect_result = self.client.connect(self.broker_host, self.broker_port, keepalive=60)
            if connect_result != 0:
                raise Exception(f"Connect returned error code: {connect_result}")
            
            self.client.loop_start()
            
            # Wait for connection to establish
            connection_timeout = 10
            for i in range(connection_timeout):
                if self.connected:
                    break
                print(f"⏳ [Controller] Waiting for connection... ({i+1}/{connection_timeout})")
                time.sleep(1)
            
            if not self.connected:
                raise Exception("Failed to establish MQTT connection within timeout")
                
            print(f"✅ [Controller] Ready to collect data for {self.duration} seconds")
            
        except Exception as e:
            print(f"❌ [Controller Error] MQTT setup failed: {e}")
            return {"error": str(e), "job_id": self.job_id}

        # Collect data for specified duration
        start_time = time.time()
        last_count = 0
        
        while time.time() - start_time < self.duration:
            current_count = self.message_count
            if current_count > last_count:
                print(f"📈 [Controller] Messages received: {current_count}")
                last_count = current_count
            time.sleep(5)  # Check every 5 seconds

        print(f"⏹️  [Controller] Data collection finished. Total messages: {self.message_count}")

        # Shutdown MQTT and monitoring
        self.client.loop_stop()
        self.client.disconnect()
        monitor_thread.join(timeout=3)

        # Gather stats with debug info
        latency_stats = self.latency_tracker.get_stats()
        throughput_value = self.throughput_tracker.get_throughput(self.duration)
        availability_stats = self.availability_monitor.get_stats()
        
        print(f"📊 [Controller] Latency stats: {latency_stats}")
        print(f"📊 [Controller] Throughput: {throughput_value} msg/s")
        print(f"📊 [Controller] Availability: {availability_stats}")

        self.aggregator.add_module_stats("latency", latency_stats)
        self.aggregator.add_module_stats("throughput", {
            "throughput_mps": throughput_value
        })
        self.aggregator.add_module_stats("availability", availability_stats)

        # Save + return summary
        result = self.aggregator.get_summary()
        print(f"💾 [Controller] Evaluation complete. Summary saved to {result['saved_to']}")
        return result["summary"]
