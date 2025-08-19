import threading
import time
import uuid
import json
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
        self.connected = False
        self.message_count = 0
        self.connection_error = None  # ADD THIS LINE

        # Trackers
        self.latency_tracker = LatencyTracker()
        self.throughput_tracker = ThroughputTracker()
        self.availability_monitor = AvailabilityMonitor()

        # MQTT - Create unique client ID
        client_id = f"eval_controller_{self.job_id[:8]}"
        self.client = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv311)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.on_disconnect = self.on_disconnect
        self.client.on_subscribe = self.on_subscribe

        # Aggregator
        self.aggregator = StatsAggregator(self.job_id, self.broker_host, output_dir=output_dir)

    def on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            print(f"✅ [Controller] Connected to {self.broker_host}:{self.broker_port}")
            self.connected = True
            self.connection_error = None  # Clear any previous errors
            # Subscribe with QoS 1 for reliability
            result, mid = client.subscribe("sim/stats/delay", qos=1)
            print(f"✅ [Controller] Subscribe request sent (mid={mid})")
        else:
            error_msg = f"Connection failed with code {rc}"
            print(f"❌ [Controller] {error_msg}")
            self.connected = False
            self.connection_error = error_msg

    def on_subscribe(self, client, userdata, mid, granted_qos):
        print(f"✅ [Controller] Successfully subscribed to sim/stats/delay (mid={mid}, qos={granted_qos})")

    def on_disconnect(self, client, userdata, rc):
        print(f"🔌 [Controller] Disconnected (code: {rc})")
        self.connected = False
        if rc != 0:
            print(f"⚠️  [Controller] Unexpected disconnection!")

    def on_message(self, client, userdata, msg):
        try:
            self.message_count += 1
            print(f"📨 [Controller] Message #{self.message_count} received on {msg.topic}")
            
            if msg.topic == "sim/stats/delay":
                # Process the message
                self.latency_tracker.handle_message(msg)
                self.throughput_tracker.record_message()
                
                # Debug: Show what we received
                try:
                    payload_str = msg.payload.decode() if isinstance(msg.payload, bytes) else str(msg.payload)
                    payload = json.loads(payload_str)
                    print(f"   📊 Delay: {payload.get('delay', 'N/A')}ms, From: {payload.get('name', 'unknown')}, Topic: {payload.get('subscriber_topic', 'unknown')}")
                except Exception as e:
                    print(f"   ⚠️  Payload parse error: {e}")
                    print(f"   ⚠️  Raw payload: {msg.payload}")
                    
        except Exception as e:
            print(f"❌ [Controller] Message handling error: {e}")
            import traceback
            traceback.print_exc()

    def run(self):
        print(f"🚀 [Controller] Starting evaluation for {self.duration}s")
        print(f"   Broker: {self.broker_host}:{self.broker_port}")
        print(f"   Job ID: {self.job_id}")
        
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
            
            # Set up callbacks before connecting
            self.client.loop_start()
            
            # Connect with error handling
            connect_result = self.client.connect(self.broker_host, self.broker_port, keepalive=60)
            if connect_result != 0:
                raise Exception(f"Connect returned error code: {connect_result}")
            
            # Wait for connection to establish
            connection_timeout = 10
            for i in range(connection_timeout):
                if self.connected:
                    break
                if self.connection_error:
                    raise Exception(f"Connection error: {self.connection_error}")
                print(f"⏳ [Controller] Waiting for connection... ({i+1}/{connection_timeout})")
                time.sleep(1)
            
            if not self.connected:
                raise Exception("Failed to establish MQTT connection within timeout")
                
            print(f"✅ [Controller] Ready to collect data for {self.duration} seconds")
            
            # IMPORTANT: Wait for subscription to complete and messages to start flowing
            print("⏳ [Controller] Waiting for message flow to stabilize...")
            time.sleep(5)
            
        except Exception as e:
            print(f"❌ [Controller Error] MQTT setup failed: {e}")
            self.client.loop_stop()
            return {"error": str(e), "job_id": self.job_id}

        # Collect data for specified duration
        start_time = time.time()
        last_count = 0
        last_report_time = start_time
        no_message_warnings = 0
        
        while time.time() - start_time < self.duration:
            current_time = time.time()
            current_count = self.message_count
            elapsed = int(current_time - start_time)
            
            # Report progress every 5 seconds
            if current_time - last_report_time >= 5:
                rate = (current_count - last_count) / (current_time - last_report_time)
                remaining = self.duration - elapsed
                
                print(f"📈 [Controller] Progress: {elapsed}s/{self.duration}s | Messages: {current_count} | Rate: {rate:.1f} msg/s | Remaining: {remaining}s")
                
                # Warn if no messages after 10 seconds
                if current_count == 0 and elapsed >= 10:
                    no_message_warnings += 1
                    print(f"⚠️  [Controller] WARNING: No messages received after {elapsed} seconds!")
                    if no_message_warnings == 1:
                        print("   Possible issues:")
                        print("   - Check if publishers are deployed and running")
                        print("   - Verify broker connectivity")
                        print("   - Ensure topics match between publishers and subscribers")
                        print("   - Check Node-RED logs for errors")
                
                last_count = current_count
                last_report_time = current_time
                
            time.sleep(1)

        print(f"⏹️  [Controller] Data collection finished. Total messages: {self.message_count}")

        # Shutdown MQTT and monitoring
        self.client.loop_stop()
        self.client.disconnect()
        monitor_thread.join(timeout=3)

        # Gather stats with debug info
        latency_stats = self.latency_tracker.get_stats()
        throughput_value = self.throughput_tracker.get_throughput(self.duration)
        availability_stats = self.availability_monitor.get_stats()
        
        print(f"\n📊 [Controller] Final Statistics:")
        print(f"   Total messages processed: {self.message_count}")
        print(f"   Latency stats: {latency_stats}")
        print(f"   Throughput: {throughput_value} msg/s")
        print(f"   Availability: {availability_stats}")

        # Check if we got any data
        if self.message_count == 0:
            print("\n❌ [Controller] ERROR: No messages were received during evaluation!")
            print("   This indicates a problem with the simulation setup.")
            
            # Add diagnostic information to the results
            latency_stats['diagnostic_info'] = {
                'no_messages_received': True,
                'evaluation_duration': self.duration,
                'broker_host': self.broker_host,
                'broker_port': self.broker_port
            }

        self.aggregator.add_module_stats("latency", latency_stats)
        self.aggregator.add_module_stats("throughput", {
            "throughput_mps": throughput_value,
            "total_messages": self.message_count
        })
        self.aggregator.add_module_stats("availability", availability_stats)

        # Save + return summary
        result = self.aggregator.get_summary()
        print(f"💾 [Controller] Evaluation complete. Summary saved to {result['saved_to']}")
        return result["summary"]