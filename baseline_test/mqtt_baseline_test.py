#!/usr/bin/env python3
"""
Standalone MQTT Baseline Test - Run on Remote Machine
Compares pure Python MQTT clients vs Node-RED for overhead assessment.

Usage:
    python mqtt_baseline_test.py --broker 10.22.110.12 --duration 60
"""

import argparse
import json
import threading
import time
import statistics
from collections import deque
from datetime import datetime

import paho.mqtt.client as mqtt

# Handle both paho-mqtt v1.x and v2.x
try:
    from paho.mqtt.enums import CallbackAPIVersion
    PAHO_V2 = True
except ImportError:
    PAHO_V2 = False

# =============================================================================
# CONFIGURATION
# =============================================================================
DEFAULT_CONFIG = {
    "broker_host": "10.22.110.12",
    "broker_port": 1883,
    "num_publishers": 10,
    "num_subscribers": 10,
    "qos": 1,
    "topic_prefix": "benchmark/baseline",
    "message_rate_hz": 10,  # Messages per second per publisher
    "duration_seconds": 60,
    "warmup_seconds": 5,
    "payload_size": 64  # Bytes (approximate)
}


# =============================================================================
# LATENCY TRACKER
# =============================================================================
class LatencyTracker:
    def __init__(self):
        self.lock = threading.Lock()
        self.latencies = deque()
        self.message_count = 0
        self.error_count = 0
        self.start_time = None
        self.end_time = None
    
    def record(self, latency_ms):
        with self.lock:
            if latency_ms >= 0 and latency_ms < 60000:
                self.latencies.append(latency_ms)
                self.message_count += 1
            else:
                self.error_count += 1
    
    def start(self):
        self.start_time = time.time()
    
    def stop(self):
        self.end_time = time.time()
    
    def get_stats(self):
        with self.lock:
            if not self.latencies:
                return {"count": 0, "error": "No data collected"}
            
            latencies = list(self.latencies)
            duration = (self.end_time - self.start_time) if self.end_time else 0
            
            return {
                "count": len(latencies),
                "mean_ms": round(statistics.mean(latencies), 2),
                "min_ms": round(min(latencies), 2),
                "max_ms": round(max(latencies), 2),
                "stddev_ms": round(statistics.stdev(latencies), 2) if len(latencies) > 1 else 0,
                "p50_ms": round(statistics.median(latencies), 2),
                "p95_ms": round(sorted(latencies)[int(len(latencies) * 0.95)], 2) if len(latencies) >= 20 else 0,
                "p99_ms": round(sorted(latencies)[int(len(latencies) * 0.99)], 2) if len(latencies) >= 100 else 0,
                "throughput_mps": round(len(latencies) / duration, 2) if duration > 0 else 0,
                "duration_s": round(duration, 2),
                "error_count": self.error_count
            }


# =============================================================================
# PUBLISHER
# =============================================================================
class Publisher(threading.Thread):
    def __init__(self, pub_id, config, stop_event):
        super().__init__(daemon=True)
        self.pub_id = pub_id
        self.config = config
        self.stop_event = stop_event
        self.topic = f"{config['topic_prefix']}/pub{pub_id}"
        self.messages_sent = 0
        self.client = None
    
    def run(self):
        if PAHO_V2:
            self.client = mqtt.Client(
                callback_api_version=CallbackAPIVersion.VERSION2,
                client_id=f"baseline_pub_{self.pub_id}"
            )
        else:
            self.client = mqtt.Client(client_id=f"baseline_pub_{self.pub_id}")
        self.client.connect(self.config["broker_host"], self.config["broker_port"])
        self.client.loop_start()
        
        interval = 1.0 / self.config["message_rate_hz"]
        seq = 0
        
        while not self.stop_event.is_set():
            timestamp = int(time.time() * 1000)  # Unix timestamp in ms
            payload = json.dumps({
                "t": timestamp,
                "pub": f"pub{self.pub_id}",
                "seq": seq,
                "data": "x" * (self.config["payload_size"] - 50)  # Padding
            })
            
            self.client.publish(self.topic, payload, qos=self.config["qos"])
            self.messages_sent += 1
            seq += 1
            
            time.sleep(interval)
        
        self.client.loop_stop()
        self.client.disconnect()


# =============================================================================
# SUBSCRIBER
# =============================================================================
class Subscriber(threading.Thread):
    def __init__(self, sub_id, config, stop_event, tracker):
        super().__init__(daemon=True)
        self.sub_id = sub_id
        self.config = config
        self.stop_event = stop_event
        self.tracker = tracker
        self.topic_pattern = f"{config['topic_prefix']}/#"
        self.messages_received = 0
        self.client = None
    
    def on_message(self, client, userdata, msg):
        try:
            receive_time = int(time.time() * 1000)
            data = json.loads(msg.payload.decode())
            send_time = data.get("t", 0)
            latency = receive_time - send_time
            self.tracker.record(latency)
            self.messages_received += 1
        except Exception as e:
            self.tracker.error_count += 1
    
    def run(self):
        if PAHO_V2:
            self.client = mqtt.Client(
                callback_api_version=CallbackAPIVersion.VERSION2,
                client_id=f"baseline_sub_{self.sub_id}"
            )
        else:
            self.client = mqtt.Client(client_id=f"baseline_sub_{self.sub_id}")
        self.client.on_message = self.on_message
        self.client.connect(self.config["broker_host"], self.config["broker_port"])
        self.client.subscribe(self.topic_pattern, qos=self.config["qos"])
        self.client.loop_start()
        
        while not self.stop_event.is_set():
            time.sleep(0.1)
        
        self.client.loop_stop()
        self.client.disconnect()


# =============================================================================
# MAIN TEST RUNNER
# =============================================================================
def run_baseline_test(config):
    print("=" * 60)
    print("MQTT BASELINE TEST - Pure Python Clients")
    print("=" * 60)
    print(f"Broker: {config['broker_host']}:{config['broker_port']}")
    print(f"Publishers: {config['num_publishers']}")
    print(f"Subscribers: {config['num_subscribers']}")
    print(f"QoS: {config['qos']}")
    print(f"Message Rate: {config['message_rate_hz']} msg/s per publisher")
    print(f"Duration: {config['duration_seconds']}s (+ {config['warmup_seconds']}s warmup)")
    print("=" * 60)
    
    tracker = LatencyTracker()
    stop_event = threading.Event()
    
    # Start subscribers first
    print("\n[1] Starting subscribers...")
    subscribers = []
    for i in range(config["num_subscribers"]):
        sub = Subscriber(i, config, stop_event, tracker)
        sub.start()
        subscribers.append(sub)
    
    time.sleep(1)  # Let subscribers connect
    
    # Start publishers
    print("[2] Starting publishers...")
    publishers = []
    for i in range(config["num_publishers"]):
        pub = Publisher(i, config, stop_event)
        pub.start()
        publishers.append(pub)
    
    # Warmup period
    print(f"[3] Warming up for {config['warmup_seconds']}s...")
    time.sleep(config["warmup_seconds"])
    
    # Reset tracker for actual measurement
    tracker = LatencyTracker()
    for sub in subscribers:
        sub.tracker = tracker
    
    # Start measurement
    print(f"[4] Running test for {config['duration_seconds']}s...")
    tracker.start()
    
    # Progress updates
    start = time.time()
    while time.time() - start < config["duration_seconds"]:
        elapsed = int(time.time() - start)
        print(f"\r    Progress: {elapsed}/{config['duration_seconds']}s | Messages: {tracker.message_count}", end="", flush=True)
        time.sleep(1)
    
    print()  # Newline
    
    # Stop everything
    tracker.stop()
    stop_event.set()
    
    # Wait for threads to finish
    for pub in publishers:
        pub.join(timeout=2)
    for sub in subscribers:
        sub.join(timeout=2)
    
    # Calculate results
    print("\n[5] Computing statistics...")
    stats = tracker.get_stats()
    
    # Calculate total messages sent
    total_sent = sum(pub.messages_sent for pub in publishers)
    total_received = sum(sub.messages_received for sub in subscribers)
    
    # Print results
    print("\n" + "=" * 60)
    print("RESULTS")
    print("=" * 60)
    print(f"Messages Sent:      {total_sent}")
    print(f"Messages Received:  {total_received}")
    print(f"Latency Samples:    {stats['count']}")
    print(f"Mean Latency:       {stats['mean_ms']} ms")
    print(f"Min Latency:        {stats['min_ms']} ms")
    print(f"Max Latency:        {stats['max_ms']} ms")
    print(f"Jitter (StdDev):    {stats['stddev_ms']} ms")
    print(f"P50 Latency:        {stats['p50_ms']} ms")
    print(f"P95 Latency:        {stats['p95_ms']} ms")
    print(f"P99 Latency:        {stats['p99_ms']} ms")
    print(f"Throughput:         {stats['throughput_mps']} msg/s")
    print(f"Errors:             {stats['error_count']}")
    print("=" * 60)
    
    # Save results to file
    result_file = f"baseline_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    results = {
        "config": config,
        "stats": stats,
        "total_sent": total_sent,
        "total_received": total_received,
        "timestamp": datetime.now().isoformat()
    }
    
    with open(result_file, 'w') as f:
        json.dump(results, f, indent=2)
    
    print(f"\nResults saved to: {result_file}")
    return results


# =============================================================================
# ENTRY POINT
# =============================================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MQTT Baseline Latency Test")
    parser.add_argument("--broker", default=DEFAULT_CONFIG["broker_host"], help="Broker IP address")
    parser.add_argument("--port", type=int, default=DEFAULT_CONFIG["broker_port"], help="Broker port")
    parser.add_argument("--publishers", type=int, default=DEFAULT_CONFIG["num_publishers"], help="Number of publishers")
    parser.add_argument("--subscribers", type=int, default=DEFAULT_CONFIG["num_subscribers"], help="Number of subscribers")
    parser.add_argument("--qos", type=int, default=DEFAULT_CONFIG["qos"], choices=[0, 1, 2], help="QoS level")
    parser.add_argument("--rate", type=int, default=DEFAULT_CONFIG["message_rate_hz"], help="Messages per second per publisher")
    parser.add_argument("--duration", type=int, default=DEFAULT_CONFIG["duration_seconds"], help="Test duration in seconds")
    parser.add_argument("--warmup", type=int, default=DEFAULT_CONFIG["warmup_seconds"], help="Warmup duration in seconds")
    
    args = parser.parse_args()
    
    config = {
        "broker_host": args.broker,
        "broker_port": args.port,
        "num_publishers": args.publishers,
        "num_subscribers": args.subscribers,
        "qos": args.qos,
        "topic_prefix": "benchmark/baseline",
        "message_rate_hz": args.rate,
        "duration_seconds": args.duration,
        "warmup_seconds": args.warmup,
        "payload_size": 64
    }
    
    run_baseline_test(config)
