# Standard library imports
import csv
import json
import os
from dotenv import load_dotenv
import statistics
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from threading import Thread

# Third-party imports
import docker
import paho.mqtt.client as mqtt
import requests
from flask import Flask, render_template, request, send_from_directory, jsonify
from paho.mqtt.enums import CallbackAPIVersion

# Local imports
from deployment.topic_manager import TopicManager
from deployment.group_expander import GroupExpander
from evaluation.controller import EvaluationController


# Load environment variables
load_dotenv()

# Configuration for Docker networking
class Config:
    # When Flask is on host, Node-RED/NanoMQ in Docker
    DOCKER_MQTT_HOST = os.getenv('DOCKER_MQTT_HOST', 'nanomq')  # Docker service name
    DOCKER_NODE_RED_URL = os.getenv('DOCKER_NODE_RED_URL', 'http://nodered:1880')
    
    # For host access
    HOST_MQTT_HOST = os.getenv('HOST_MQTT_HOST', 'localhost')
    HOST_MQTT_PORT = int(os.getenv('HOST_MQTT_PORT', 1883))
    NODE_RED_URL = os.getenv('NODE_RED_URL', 'http://localhost:1880')

# Update the global NODE_RED_URL
NODE_RED_URL = Config.NODE_RED_URL

# Flask app initialization
app = Flask(__name__, template_folder="frontend/templates", static_folder="frontend/static")

# Global variables
delay_data = deque(maxlen=2000)
job_status = {}

# Configuration constants
NODE_RED_URL = 'http://localhost:1880'

# Broker container IDs
def get_broker_container(broker_name):
    try:
        client = docker.from_env()
        return client.containers.get(broker_name)
    except docker.errors.NotFound:
        return None
    except Exception as e:
        print(f"Docker error: {e}")
        return None

# Initialize topic manager
topic_manager = TopicManager()

def new_id():
    """Generate a new 8-character hex ID"""
    return uuid.uuid4().hex[:8]

# =============================================================================
# TOPIC MANAGEMENT ROUTES
# =============================================================================

@app.route('/topics', methods=['GET'])
def get_topics():
    """Get all available topics"""
    return jsonify(topic_manager.get_all_topics())

@app.route('/topics', methods=['POST'])
def add_topic_or_group():
    """Add a single topic or create a topic group"""
    data = request.get_json()
    if 'group_name' in data:
        topic_manager.create_group(data['group_name'], int(data.get('count', 1)))
    elif 'topic' in data:
        topic_manager.add_topic(data['topic'])
    else:
        return jsonify({"error": "Missing topic or group_name"}), 400
    return jsonify({"ok": True})

@app.route('/topics/<name>', methods=['DELETE'])
def delete_topic(name):
    """Delete a specific topic"""
    if topic_manager.remove_topic(name):
        return jsonify({"ok": True})
    else:
        return jsonify({"error": "Topic not found"}), 404

@app.route('/topics', methods=['DELETE'])
def reset_topics():
    """Reset all topics and groups"""
    topic_manager.reset()
    return jsonify({"ok": True})

# =============================================================================
# GROUP EXPANSION ROUTES
# =============================================================================

@app.route('/expand_groups', methods=['POST'])
def expand_groups():
    """Expand groups into individual instances"""
    data = request.get_json()
    kind = data.get('kind')  # "publisher" or "subscriber"
    groups = data.get('groups', [])

    if kind not in ("publisher", "subscriber"):
        return jsonify({"error": "kind must be 'publisher' or 'subscriber'"}), 400

    expander = GroupExpander(mode=kind)
    instances, warnings = expander.expand(groups)
    return jsonify({
        "instances": instances,
        "warnings": warnings
    })

# =============================================================================
# SIMULATION DEPLOYMENT ROUTES
# =============================================================================

# Replace your deploy_simulation function with this fixed version:

@app.route('/deploy_simulation', methods=['POST'])
def deploy_simulation():
    """Deploy simulation flows to Node-RED"""
    data = request.get_json()

    publisher_groups = data.get("publisher_groups", [])
    subscriber_groups = data.get("subscriber_groups", []) 

    # Get broker configuration
    broker_host = data.get("broker_name", "localhost")
    broker_port = int(data.get("broker_port", 1883))

    if broker_host.lower() in ['localhost', '127.0.0.1']:
        # When Node-RED is in Docker and needs to connect to host
        # Use the Docker service name if both are in Docker
        broker_host_for_nodered = Config.DOCKER_MQTT_HOST  # This will be 'nanomq'
    elif broker_host.lower() in get_docker_broker_names():
        # If it's a known Docker broker, use the container name
        broker_host_for_nodered = broker_host.lower()
    else:
        # For external brokers, use as-is
        broker_host_for_nodered = broker_host
    
    print(f"🔧 [Docker Fix] Node-RED will connect to: {broker_host_for_nodered}:{broker_port}")
    print(f"🔧 [Docker Fix] Flask will connect to: {Config.HOST_MQTT_HOST}:{broker_port}")

    # Expand groups into individual instances
    pub_expander = GroupExpander(mode="publisher")
    pub_instances, pub_warnings = pub_expander.expand(publisher_groups)

    sub_expander = GroupExpander(mode="subscriber")
    sub_instances, sub_warnings = sub_expander.expand(subscriber_groups)

    # Build Node-RED flow
    all_nodes = []

    # Create main tab
    tab_id = new_id()
    all_nodes.append({
        "id": tab_id,
        "type": "tab",
        "label": f"Sim-{tab_id[:6]}",
        "disabled": False,
        "info": ""
    })

    # MQTT broker configuration node
    broker_config_id = new_id()
    all_nodes.append({
        "id": broker_config_id,
        "type": "mqtt-broker",
        "name": f"{broker_host}_config",
        "broker": broker_host_for_nodered,
        "port": broker_port,
        "clientid": "",
        "usetls": False,
        "protocolVersion": 4,
        "keepalive": 60,
        "cleansession": True
    })

    # Collect all topics that are actually being published to
    published_topics = set()
    for pub in pub_instances:
        published_topics.add(pub["topic"])

    # Add publisher nodes
    y = 80
    for pub in pub_instances:
        inject_id = new_id()
        function_id = new_id()
        mqtt_id = new_id()

        all_nodes.extend([
            {
                "id": inject_id,
                "type": "inject",
                "z": tab_id,
                "name": pub["name"],
                "props": [{"p":"payload"}, {"p":"topic", "vt":"str"}],
                "repeat": str(pub.get("interval", 1.0)),
                "once": True,
                "onceDelay": 0.1,
                "topic": "",
                "payload": "",
                "payloadType": "date",
                "x": 140,
                "y": y,
                "wires": [[function_id]]
            },
            {
                "id": function_id,
                "type": "function",
                "z": tab_id,
                "name": f"{pub['name']} Payload",
                "func": (
                    "// Initialize sequence counter\n"
                    "if (!global.get('seq')) global.set('seq', {});\n"
                    f"var topic = '{pub['topic']}';\n"
                    "if (!global.get('seq')[topic]) global.get('seq')[topic] = 0;\n"
                    "global.get('seq')[topic]++;\n"
                    "\n"
                    "// Create payload with timestamp for latency measurement\n"
                    "msg.payload = {\n"
                    "  ts_sent: Date.now(),\n"
                    "  seq_id: global.get('seq')[topic],\n"
                    f"  name: '{pub['name']}',\n"
                    f"  topic: '{pub['topic']}',\n"
                    f"  data: 'X'.repeat({pub.get('payload_size', 256)})\n"
                    "};\n"
                    "\n"
                    "// Set topic\n"
                    f"msg.topic = '{pub['topic']}';\n"
                    "\n"
                    "// Debug logging\n"
                    "node.log('Publishing #' + global.get('seq')[topic] + ' to ' + topic);\n"
                    "return msg;"
                ),
                "outputs": 1,
                "timeout": 0,
                "noerr": 0,
                "initialize": "",
                "finalize": "",
                "libs": [],
                "x": 340,
                "y": y,
                "wires": [[mqtt_id]]
            },
            {
                "id": mqtt_id,
                "type": "mqtt out",
                "z": tab_id,
                "name": f"{pub['name']} → {pub['topic']}",
                "topic": pub["topic"],
                "qos": str(pub.get("qos", 1)),
                "retain": str(pub.get("retain", False)).lower(),
                "respTopic": "",
                "contentType": "",
                "userProps": "",
                "correl": "",
                "expiry": "",
                "broker": broker_config_id,
                "x": 560,
                "y": y,
                "wires": []
            }
        ])
        y += 60

    # Add subscriber nodes - ONLY subscribe to topics that are actually published
    for sub in sub_instances:
        # Filter topics to only those that are actually being published
        valid_topics = [t for t in sub["topics"] if t in published_topics]
        
        if not valid_topics:
            print(f"⚠️  Warning: Subscriber {sub['name']} has no valid topics to subscribe to")
            continue
            
        for topic in valid_topics:
            mqtt_in_id = new_id()
            delay_func_id = new_id()
            mqtt_out_id = new_id()

            all_nodes.extend([
                {
                    "id": mqtt_in_id,
                    "type": "mqtt in",
                    "z": tab_id,
                    "name": f"{sub['name']} ← {topic}",
                    "topic": topic,
                    "qos": str(sub.get("qos", 1)),
                    "datatype": "json",
                    "broker": broker_config_id,
                    "nl": False,
                    "rap": True,
                    "rh": 0,
                    "inputs": 0,
                    "x": 100,
                    "y": y,
                    "wires": [[delay_func_id]]
                },
                {
                    "id": delay_func_id,
                    "type": "function",
                    "z": tab_id,
                    "name": f"{sub['name']} DelayCalc",
                    "func": (
                        "// Calculate message delay\n"
                        "try {\n"
                        "  var payload = msg.payload;\n"
                        "  \n"
                        "  // Handle both string and object payloads\n"
                        "  if (typeof payload === 'string') {\n"
                        "    try {\n"
                        "      payload = JSON.parse(payload);\n"
                        "    } catch (e) {\n"
                        "      node.error('Failed to parse payload as JSON: ' + e.message);\n"
                        "      return null;\n"
                        "    }\n"
                        "  }\n"
                        "  \n"
                        "  // Validate payload\n"
                        "  if (!payload || typeof payload !== 'object') {\n"
                        "    node.error('Invalid payload type: ' + typeof payload);\n"
                        "    return null;\n"
                        "  }\n"
                        "  \n"
                        "  if (!payload.ts_sent) {\n"
                        "    node.error('Missing ts_sent in payload: ' + JSON.stringify(payload));\n"
                        "    return null;\n"
                        "  }\n"
                        "  \n"
                        "  // Calculate delay\n"
                        "  var now = Date.now();\n"
                        "  var delay = now - payload.ts_sent;\n"
                        "  \n"
                        "  // Create output message for stats collector\n"
                        "  var output = {\n"
                        "    topic: 'sim/stats/delay',\n"
                        "    payload: JSON.stringify({\n"
                        f"      name: '{sub['name']}',\n"
                        f"      subscriber_topic: '{topic}',\n"
                        "      delay: delay,\n"
                        "      seq_id: payload.seq_id || 0,\n"
                        "      ts_sent: payload.ts_sent,\n"
                        "      ts_recv: now,\n"
                        "      publisher_name: payload.name || 'unknown',\n"
                        "      original_topic: payload.topic || msg.topic\n"
                        "    })\n"
                        "  };\n"
                        "  \n"
                        "  // Log success\n"
                        "  node.log('Delay ' + delay + 'ms for seq ' + payload.seq_id + ' from ' + payload.name);\n"
                        "  return output;\n"
                        "  \n"
                        "} catch (error) {\n"
                        "  node.error('Error calculating delay: ' + error.message + ', payload: ' + JSON.stringify(msg.payload));\n"
                        "  return null;\n"
                        "}"
                    ),
                    "outputs": 1,
                    "timeout": 0,
                    "noerr": 0,
                    "initialize": "",
                    "finalize": "",
                    "libs": [],
                    "x": 320,
                    "y": y,
                    "wires": [[mqtt_out_id]]
                },
                {
                    "id": mqtt_out_id,
                    "type": "mqtt out",
                    "z": tab_id,
                    "name": f"→ sim/stats/delay",
                    "topic": "sim/stats/delay",
                    "qos": "1",
                    "retain": "false",
                    "respTopic": "",
                    "contentType": "",
                    "userProps": "",
                    "correl": "",
                    "expiry": "",
                    "broker": broker_config_id,
                    "x": 550,
                    "y": y,
                    "wires": []
                }
            ])
            y += 80

    # Deploy flows to Node-RED
    try:
        print(f"🚀 Deploying {len(all_nodes)} nodes to Node-RED...")
        print(f"   Published topics: {published_topics}")
        print(f"   Valid subscriber connections: {sum(len([t for t in sub['topics'] if t in published_topics]) for sub in sub_instances)}")
        
        # Use 'flows' instead of 'full' to preserve existing MQTT connections
        resp = requests.post(
            f'{NODE_RED_URL}/flows',
            headers={'Content-Type': 'application/json', 'Node-RED-Deployment-Type': 'flows'},
            json=all_nodes,
            timeout=30
        )
        
        if resp.status_code == 204:
            print("✅ Successfully deployed to Node-RED")
            print(f"   📊 Publishers: {len(pub_instances)}")
            print(f"   📊 Subscribers: {len(sub_instances)}")
            print(f"   📊 Topics: {len(published_topics)}")
            
            # Wait a moment for flows to start, then restart delay collector
            time.sleep(2)
            restart_delay_collector()
            
            # Add warnings about topic mismatches
            all_warnings = pub_warnings + sub_warnings
            
            # Check for subscribers with no matching publishers
            for sub in sub_instances:
                unmatched = [t for t in sub['topics'] if t not in published_topics]
                if unmatched:
                    all_warnings.append(f"Subscriber '{sub['name']}' is trying to listen to non-existent topics: {unmatched}")
            
            return jsonify(ok=True, warnings=all_warnings)
        else:
            print(f"❌ Node-RED deployment failed: {resp.status_code} - {resp.text}")
            return jsonify(error=f"Failed to deploy: {resp.text}"), 500
            
    except requests.RequestException as e:
        print(f"❌ Node-RED connection failed: {str(e)}")
        return jsonify(error=f"Node-RED connection failed: {str(e)}"), 500
    
# =============================================================================
# SIMULATION CONTROL ROUTES
# =============================================================================

@app.route('/simulation/<action>', methods=['POST'])
def control_simulation(action):
    """Start or stop the simulation by toggling inject nodes"""
    if action not in ('start', 'stop'):
        return jsonify({"error": "Invalid action"}), 400

    try:
        # Get all current flows
        flows_resp = requests.get(f'{NODE_RED_URL}/flows', timeout=10)
        flows_resp.raise_for_status()
        flows = flows_resp.json()

        # Toggle inject nodes
        for node in flows:
            if node.get("type") == "inject":
                node["disabled"] = (action == "stop")

        # Redeploy with updated inject states
        resp = requests.post(
            f'{NODE_RED_URL}/flows',
            headers={'Content-Type': 'application/json'},
            json=flows,
            timeout=10
        )
        resp.raise_for_status()

        if resp.status_code == 204:
            return jsonify(ok=True, action=action)
        else:
            return jsonify(error=resp.text), 500

    except requests.RequestException as e:
        return jsonify(error=f"Node-RED operation failed: {str(e)}"), 500

# =============================================================================
# MQTT DELAY COLLECTION
# =============================================================================

def restart_delay_collector():
    """Restart the delay collector to ensure it's connected after flow deployment"""
    global delay_collector_client
    
    try:
        # Stop existing collector if running
        if delay_collector_client:
            try:
                delay_collector_client.loop_stop()
                delay_collector_client.disconnect()
            except:
                pass
        
        # Start new collector
        print("🔄 Restarting delay collector...")
        delay_collector_client = start_delay_collector('localhost', 1883, delay_data)
        
        if delay_collector_client:
            print("✅ Delay collector restarted successfully")
            return True
        else:
            print("❌ Failed to restart delay collector")
            return False
            
    except Exception as e:
        print(f"❌ Error restarting delay collector: {e}")
        return False

def start_delay_collector(broker_host, broker_port, delay_deque):
    """Start MQTT client to collect delay measurements"""
    # Flask on host should always connect to localhost
    actual_broker_host = Config.HOST_MQTT_HOST if broker_host in ['nanomq', 'mosquitto'] else broker_host
    
    print(f"🔗 Connecting delay collector to {actual_broker_host}:{broker_port}")
    # Use the new callback API version
    client = mqtt.Client(
        callback_api_version=CallbackAPIVersion.VERSION2,
        client_id=f"flask_delay_collector_{uuid.uuid4().hex[:8]}",
        protocol=mqtt.MQTTv311
    )
    
    # Store connection state
    client.connected = False
    client.reconnect_delay = 5

    try:
        client.connect(actual_broker_host, broker_port, 60)
        client.loop_start()
        print(f"🔄 Delay collector loop started for {actual_broker_host}:{broker_port}")
        return client
    except Exception as e:
        print(f"❌ Failed to connect delay collector: {e}")
        return None
    
    def on_connect(client, userdata, flags, reason_code, properties):
            if reason_code == 0:
                print(f"✅ Delay collector connected to {broker_host}:{broker_port}")
                client.connected = True
                result = client.subscribe("sim/stats/delay", qos=1)
                print("✅ Subscribed to sim/stats/delay")
            else:
                print(f"❌ Delay collector connection failed: {reason_code}")
                client.connected = False
        
    # Replace the on_message function in start_delay_collector with this:

    def on_message(client, userdata, msg):
        try:
            payload_str = msg.payload.decode('utf-8') if isinstance(msg.payload, bytes) else str(msg.payload)
            
            # Parse JSON payload
            payload = json.loads(payload_str)
            payload['timestamp'] = time.time()
            
            # Add to delay queue
            delay_deque.append(payload)
            
            # Enhanced logging
            delay_ms = payload.get('delay', 'N/A')
            name = payload.get('name', 'unknown')
            seq_id = payload.get('seq_id', 'N/A')
            
            print(f"📨 [DelayCollector] Received: {delay_ms}ms delay, seq:{seq_id}, from:{name}")
            
            # Also log queue size periodically
            if len(delay_deque) % 50 == 0:
                print(f"📊 [DelayCollector] Queue size: {len(delay_deque)} messages")
                
        except json.JSONDecodeError as e:
            print(f"❌ [DelayCollector] JSON decode error: {e}")
            print(f"   Raw payload: {msg.payload}")
        except Exception as e:
            print(f"❌ [DelayCollector] Message processing error: {e}")
            print(f"   Topic: {msg.topic}, Payload: {msg.payload}")
        
    def on_disconnect(client, userdata, reason_code, properties):
            print(f"🔌 [DelayCollector] Disconnected from broker (rc={reason_code})")
            client.connected = False
            if reason_code != 0:
                # Attempt reconnection after delay
                print(f"⏳ [DelayCollector] Will attempt reconnection in {client.reconnect_delay} seconds...")
                time.sleep(client.reconnect_delay)
                try:
                    client.reconnect()
                except Exception as e:
                    print(f"❌ [DelayCollector] Reconnection failed: {e}")
        
    client.on_connect = on_connect
    client.on_message = on_message
    client.on_disconnect = on_disconnect
        
        # Enable automatic reconnection
    client.reconnect_delay_set(min_delay=1, max_delay=120)
        
    try:
            print(f"🔗 Connecting delay collector to {broker_host}:{broker_port}")
            client.connect(broker_host, broker_port, 60)
            client.loop_start()
            print(f"🔄 Delay collector loop started for {broker_host}:{broker_port}")
            return client
    except Exception as e:
            print(f"❌ Failed to connect delay collector: {e}")
            return None

# Global delay collector client
delay_collector_client = None


# Add this route to test the complete flow:

@app.route('/test/complete_flow', methods=['POST'])
def test_complete_flow():
    """Test complete message flow including delay statistics"""
    try:
        # Clear existing delay data
        delay_data.clear()
        
        # Create a test client
        test_client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=f"flow_test_{uuid.uuid4().hex[:8]}"
        )
        
        test_client.connect('localhost', 1883, 60)
        
        # Publish a test message that matches Node-RED publisher format
        test_payload = {
            "ts_sent": int(time.time() * 1000),  # milliseconds
            "seq_id": 1,
            "name": "test_publisher",
            "topic": "test/flow",
            "data": "X" * 100
        }
        
        print(f"🧪 [FlowTest] Publishing test message: {test_payload}")
        
        # Publish to a topic that should have subscribers
        result = test_client.publish("test/flow", json.dumps(test_payload), qos=1)
        result.wait_for_publish()
        
        test_client.disconnect()
        
        # Wait for delay collector to process
        time.sleep(2)
        
        # Check if delay data was received
        recent_delays = list(delay_data)[-5:] if delay_data else []
        
        return jsonify({
            "success": True,
            "test_payload": test_payload,
            "delay_messages_received": len(recent_delays),
            "recent_delays": recent_delays,
            "collector_connected": bool(delay_collector_client and hasattr(delay_collector_client, 'connected') and delay_collector_client.connected)
        })
        
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500



@app.route('/api/metrics')
def get_delay_metrics():
        """Get latest delay metrics"""
        return jsonify(list(delay_data)[-100:])

    # =============================================================================
    # DOCKER MONITORING FUNCTIONS
    # =============================================================================

def monitor_container_stats(container_id, csv_path, stop_event):
        """Monitor Docker container resource usage and save to CSV"""
        try:
            client = docker.from_env()
            container = client.containers.get(container_id)

            with open(csv_path, 'w', newline='') as csvfile:
                writer = csv.writer(csvfile)
                writer.writerow([
                    'timestamp', 'cpu_percent', 'mem_usage', 'mem_limit',
                    'net_rx', 'net_tx', 'block_read', 'block_write'
                ])

                stats_gen = container.stats(stream=True, decode=True)

                while not stop_event.is_set():
                    try:
                        stats = next(stats_gen)

                        # Calculate CPU percentage
                        cpu_stats = stats.get('cpu_stats', {})
                        precpu_stats = stats.get('precpu_stats', {})

                        cpu_delta = cpu_stats.get('cpu_usage', {}).get('total_usage', 0) - \
                                    precpu_stats.get('cpu_usage', {}).get('total_usage', 0)
                        system_delta = cpu_stats.get('system_cpu_usage', 0) - \
                                    precpu_stats.get('system_cpu_usage', 0)

                        cpu_percent = (cpu_delta / system_delta) * 100 if system_delta > 0 else 0

                        # Memory statistics
                        mem_usage = stats.get('memory_stats', {}).get('usage', 0)
                        mem_limit = stats.get('memory_stats', {}).get('limit', 0)

                        # Network statistics
                        networks = stats.get('networks', {})
                        net_rx = sum(n.get('rx_bytes', 0) for n in networks.values())
                        net_tx = sum(n.get('tx_bytes', 0) for n in networks.values())

                        # Block I/O statistics
                        blkio_stats = stats.get('blkio_stats', {}).get('io_service_bytes_recursive', [])
                        block_read = sum(b.get('value', 0) for b in blkio_stats if b.get('op') == 'Read')
                        block_write = sum(b.get('value', 0) for b in blkio_stats if b.get('op') == 'Write')

                        timestamp = datetime.now().isoformat()
                        writer.writerow([
                            timestamp,
                            round(cpu_percent, 2),
                            mem_usage,
                            mem_limit,
                            net_rx,
                            net_tx,
                            block_read,
                            block_write
                        ])
                        csvfile.flush()
                        
                    except StopIteration:
                        print("[Monitor] Stats stream ended")
                        break
                    except Exception as e:
                        print(f"[Monitor Error] {e}")
                        time.sleep(1)
                        
        except docker.errors.NotFound:
            print(f"[Monitor Setup Failed] Container {container_id} not found")
        except Exception as e:
            print(f"[Monitor Setup Failed] {e}")
        finally:
            stop_event.set()

    # =============================================================================
    # EVALUATION AND TESTING ROUTES
    # =============================================================================

def get_docker_broker_names():
        """Return a set of container names that are known brokers"""
        return {'activemq', 'mosquitto', 'vernemq', 'emqx', 'hivemq', 'nanomq', 'rabbitmq'}

def run_tests_in_background(job_id, args):
        try:
            broker_name = args.get('broker_name', 'localhost').lower()
            broker_port = int(args.get('broker_port', 1883))
            
            # Determine MQTT connection host
            mqtt_host = 'localhost' if broker_name in get_docker_broker_names() else broker_name
            duration = int(args.get('duration', 60))

            print(f"🚀 [TestRunner] Starting evaluation for {broker_name} (job: {job_id})")
            print(f"   Host: {mqtt_host}:{broker_port}")
            print(f"   Duration: {duration}s")
            
            # Perform pre-flight checks
            print("🔍 [TestRunner] Performing pre-flight checks...")
            checks = preflight_check(mqtt_host, broker_port)
            print(f"   Broker reachable: {'✅' if checks['broker_reachable'] else '❌'}")
            print(f"   Node-RED connected: {'✅' if checks['node_red_connected'] else '❌'}")
            print(f"   Flows deployed: {'✅' if checks['flows_deployed'] else '❌'}")
            print(f"   Publishers active: {'✅' if checks['publishers_active'] else '❌'}")
            print(f"   Delay collector: {'✅' if checks['delay_collector_active'] else '❌'}")
            
            # Warn but continue if checks fail
            if not all(checks.values()):
                print("⚠️  [TestRunner] Some pre-flight checks failed. Results may be incomplete.")

            # Verify container exists (if it's a Docker broker)
            container = None
            if broker_name in get_docker_broker_names():
                container = get_broker_container(broker_name)
                if not container:
                    job_status[job_id] = {
                        'error': f'Broker container not found: {broker_name}',
                        'status': 'failed'
                    }
                    return

            # Set up resource monitoring (only for Docker brokers)
            resource_csv = None
            stop_event = None
            monitor_thread = None
            
            if container:
                resource_csv = os.path.join('results', f'resource_usage_{broker_name}_{job_id}.csv')
                stop_event = threading.Event()
                monitor_thread = threading.Thread(
                    target=monitor_container_stats,
                    args=(container.id, resource_csv, stop_event),
                    daemon=True
                )
                monitor_thread.start()

            # Run evaluation (latency, throughput, availability)
            controller = EvaluationController(
                broker_host=mqtt_host,
                broker_port=broker_port,
                duration=duration,
                job_id=job_id
            )
            
            eval_results = controller.run()
            
            if 'error' in eval_results:
                raise Exception(eval_results['error'])

            # Finish resource monitoring
            if stop_event and monitor_thread:
                stop_event.set()
                monitor_thread.join(timeout=10)

            # Save final job state
            job_status[job_id] = {
                **eval_results,
                'status': 'done',
                'monitoring': 'done' if container else 'skipped',
                'broker_name': broker_name,
                'job_id': job_id,
                'resource_csv': resource_csv
            }
            
            print(f"✅ [TestRunner] Evaluation completed for {broker_name}")
            
        except Exception as e:
            print(f"❌ [TestRunner] Error: {e}")
            import traceback
            traceback.print_exc()
            
            # Clean up monitoring if it was started
            if 'stop_event' in locals() and stop_event:
                stop_event.set()
            if 'monitor_thread' in locals() and monitor_thread:
                monitor_thread.join(timeout=5)
                
            job_status[job_id] = {
                'status': 'failed',
                'error': f'Test execution error: {str(e)}',
                'broker_name': args.get('broker_name', 'unknown')
            }

@app.route('/test/publish', methods=['POST'])
def test_publish():
    """Manually publish a test message to verify flow"""
    try:
        test_client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=f"test_publisher_{uuid.uuid4().hex[:8]}"
        )
        test_client.connect('localhost', 1883, 60)
        
        # Publish test message
        test_payload = {
            "ts_sent": time.time() * 1000,
            "seq_id": 999,
            "name": "test_publisher",
            "topic": "test/topic",
            "data": "X" * 100
        }
        
        test_client.publish("test/topic", json.dumps(test_payload), qos=1)
        test_client.disconnect()
        
        return jsonify({
            "success": True,
            "message": "Test message published",
            "payload": test_payload
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/run_tests', methods=['POST'])
def run_tests():
        """Start broker evaluation tests"""
        args = request.get_json()
        job_id = uuid.uuid4().hex
        threading.Thread(
            target=run_tests_in_background,
            args=(job_id, args),
            daemon=True
        ).start()
        return jsonify(job_id=job_id)

@app.route('/status/<job_id>')
def status(job_id):
        raw = job_status.get(job_id, {})
        # Remove unserializable fields
        clean = {k: v for k, v in raw.items() if k not in ('delay_client', 'delay_data')}
        return jsonify(clean)

@app.route('/results/<job_id>')
def results(job_id):
        stats = job_status.get(job_id, {})
        if not stats:
            return "Job not found", 404
            
        broker_name = stats.get('broker_name', 'unknown')
        resource_csv = stats.get('resource_csv')
        
        resource_data = []
        if resource_csv and os.path.exists(resource_csv):
            with open(resource_csv) as f:
                resource_data = list(csv.DictReader(f))
        
        return render_template("results.html",
            broker_name=broker_name,
            job_id=job_id,
            stats=stats,
            resource_data=json.dumps(resource_data))

    # =============================================================================
    # VERIFICATION ROUTES
    # =============================================================================
@app.route('/verify_flow', methods=['GET'])
def verify_flow():
        """Verify Node-RED flow is working"""
        try:
            # Check Node-RED flows
            resp = requests.get(f'{NODE_RED_URL}/flows', timeout=5)
            flows = resp.json()
            
            # Count nodes by type
            node_types = {}
            for node in flows:
                node_type = node.get('type', 'unknown')
                node_types[node_type] = node_types.get(node_type, 0) + 1
            
            # Check if delay collector is running
            collector_status = "Running" if delay_collector_client and hasattr(delay_collector_client, 'connected') and delay_collector_client.connected else "Not running"
            
            # Get recent delay data
            recent_delays = list(delay_data)[-10:] if delay_data else []
            
            return jsonify({
                "node_red_connected": resp.status_code == 200,
                "total_nodes": len(flows),
                "node_types": node_types,
                "delay_collector_status": collector_status,
                "recent_delay_count": len(delay_data),
                "recent_delays": recent_delays
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # =============================================================================
    # MAIN ROUTES
    # =============================================================================

@app.route('/')
def index():
        """Main application page"""
        return render_template('index.html')

@app.route('/health')
def health():
        """Health check endpoint"""
        return jsonify({
            "status": "healthy",
            "delay_collector": "running" if delay_collector_client else "not running",
            "active_jobs": len([j for j in job_status.values() if j.get('status') == 'running'])
        })

    # =============================================================================
    # ERROR HANDLERS
    # =============================================================================

@app.errorhandler(404)
def not_found(error):
        return jsonify({"error": "Not found"}), 404

@app.errorhandler(500)
def internal_error(error):
        return jsonify({"error": "Internal server error"}), 500

def preflight_check(broker_host, broker_port):
    """Perform pre-flight checks before evaluation"""
    checks = {
        "broker_reachable": False,
        "node_red_connected": False,
        "flows_deployed": False,
        "publishers_active": False,
        "delay_collector_active": False
    }
    
    # Check broker
    try:
        import socket
        sock = socket.create_connection((broker_host, broker_port), timeout=2)
        sock.close()
        checks["broker_reachable"] = True
    except:
        pass
    
    # Check Node-RED
    try:
        resp = requests.get(f'{NODE_RED_URL}/flows', timeout=2)
        if resp.status_code == 200:
            checks["node_red_connected"] = True
            flows = resp.json()
            
            # Check for simulation nodes
            has_publishers = any(n.get('type') == 'inject' and not n.get('disabled', False) for n in flows)
            has_subscribers = any(n.get('type') == 'mqtt in' for n in flows)
            checks["flows_deployed"] = has_publishers and has_subscribers
            checks["publishers_active"] = has_publishers
    except:
        pass
    
    # Check delay collector
    checks["delay_collector_active"] = bool(delay_collector_client and hasattr(delay_collector_client, 'connected') and delay_collector_client.connected)
    
    return checks

@app.route('/debug/messages', methods=['GET'])
def debug_messages():
    """Debug endpoint to check message flow"""
    return jsonify({
        "delay_collector_connected": delay_collector_client.connected if delay_collector_client else False,
        "total_delay_messages": len(delay_data),
        "recent_messages": list(delay_data)[-10:] if delay_data else [],
        "active_jobs": list(job_status.keys()),
        "timestamp": datetime.now().isoformat()
    })

@app.route('/debug/node-red', methods=['GET'])
def debug_node_red():
    """Check Node-RED flow status"""
    try:
        resp = requests.get(f'{NODE_RED_URL}/flows', timeout=5)
        flows = resp.json()
        
        # Find simulation nodes
        sim_nodes = {
            'publishers': [],
            'subscribers': [],
            'stats_publishers': []
        }
        
        for node in flows:
            if node.get('type') == 'inject':
                sim_nodes['publishers'].append({
                    'name': node.get('name', 'Unknown'),
                    'enabled': not node.get('disabled', False),
                    'repeat': node.get('repeat', 'none')
                })
            elif node.get('type') == 'mqtt in':
                sim_nodes['subscribers'].append({
                    'name': node.get('name', 'Unknown'),
                    'topic': node.get('topic', 'Unknown')
                })
            elif node.get('type') == 'mqtt out' and 'sim/stats/delay' in str(node.get('topic', '')):
                sim_nodes['stats_publishers'].append({
                    'name': node.get('name', 'Unknown'),
                    'topic': node.get('topic', 'Unknown')
                })
        
        return jsonify({
            "node_red_connected": True,
            "total_nodes": len(flows),
            "simulation_nodes": sim_nodes
        })
    except Exception as e:
        return jsonify({
            "node_red_connected": False,
            "error": str(e)
        })

    # =============================================================================
    # MAIN ENTRY POINT
    # =============================================================================

if __name__ == '__main__':
        # Ensure results directory exists
        os.makedirs('results', exist_ok=True)
        
        # Check if MQTT broker is running
        print("🔍 Checking MQTT broker connectivity...")
        test_client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id="test_connection"
        )
        try:
            test_client.connect('localhost', 1883, 60)
            test_client.disconnect()
            print("✅ MQTT broker is accessible")
        except Exception as e:
            print(f"⚠️  WARNING: Cannot connect to MQTT broker: {e}")
            print("   Make sure your MQTT broker is running on localhost:1883")
        
        # START THE DELAY COLLECTOR
        print("🚀 Starting delay collector...")
        delay_collector_client = start_delay_collector('localhost', 1883, delay_data)
        
        if delay_collector_client:
            print("✅ Delay collector started successfully")
        else:
            print("❌ WARNING: Delay collector failed to start!")
            print("   The application will continue but delay metrics won't be collected")
        
        print("🌐 Starting Flask app...")
        print("   Access the application at: http://localhost:5000")
        
        # Run Flask app
        app.run(debug=True, host='0.0.0.0', port=5000, use_reloader=False)