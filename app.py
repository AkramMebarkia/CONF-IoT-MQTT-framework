# Standard library imports
import csv
import json
import os
import statistics
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from threading import Thread

from collections import deque

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

# Flask app initialization
app = Flask(__name__, template_folder="frontend/templates")

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

# Initialize topic manager
topic_manager = TopicManager()

def new_id():
    """Generate a new 8-character hex ID"""
    return uuid.uuid4().hex[:8]

# Commented out broker reachability check due to Docker network conflicts
# def check_broker_reachability(host, port, timeout=3):
#     client = mqtt.Client()
#     try:
#         client.connect(host, port, keepalive=timeout)
#         client.disconnect()
#         return True
#     except Exception as e:
#         print(f"[BROKER CHECK] Failed to reach {host}:{port} - {e}")
#         return False

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

@app.route('/deploy_simulation', methods=['POST'])
def deploy_simulation():
    """Deploy simulation flows to Node-RED"""
    data = request.get_json()

    publisher_groups = data.get("publisher_groups", [])
    subscriber_groups = data.get("subscriber_groups", []) 

    # Get broker configuration
    broker_host = data.get("broker_name", "localhost")
    broker_port = int(data.get("broker_port", 1883))

    # Broker reachability check is commented out due to Docker network conflicts
    # if not check_broker_reachability(broker_host, broker_port):
    #     return jsonify(error=f"Cannot connect to MQTT broker at {broker_host}:{broker_port}"), 400

    # Expand groups into individual instances
    pub_expander = GroupExpander(mode="publisher")
    pub_instances, pub_warnings = pub_expander.expand(publisher_groups)

    sub_expander = GroupExpander(mode="subscriber")
    sub_instances, sub_warnings = sub_expander.expand(subscriber_groups)

    # Build Node-RED flow
    all_nodes = []
    wires = {}

    # Create main tab
    tab_id = new_id()
    all_nodes.append({
        "id": tab_id,
        "type": "tab",
        "label": "Sim-AutoFlow",
        "disabled": False,
        "info": ""
    })

    # MQTT broker configuration node
    broker_config_id = new_id()
    all_nodes.append({
        "id": broker_config_id,
        "type": "mqtt-broker",
        "name": broker_host,
        "broker": broker_host,
        "port": broker_port,
        "clientid": "",
        "usetls": False,
        "protocolVersion": 4,
        "keepalive": 60,
        "cleansession": True
    })

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
                "props": [{"p":"payload"}],
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
                    "if (!global.get('seq')) global.set('seq', {});\n"
                    "var group = msg.topic || 'default';\n"
                    "if (!global.get('seq')[group]) global.get('seq')[group] = 0;\n"
                    "global.get('seq')[group]++;\n"
                    "msg.payload = {\n"
                    f"  ts_sent: Date.now(),\n"
                    f"  seq_id: global.get('seq')[group],\n"
                    f"  data: 'X'.repeat({pub['payload_size']})\n"
                    "};\n"
                    "return msg;"
                ),
                "outputs": 1,
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
                "broker": broker_config_id,
                "x": 560,
                "y": y,
                "wires": []
            }
        ])
        y += 60

    # Add subscriber nodes
    for sub in sub_instances:
        for topic in sub["topics"]:
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
                        "if (!msg.payload.ts_sent) return null;\n"
                        "const now = Date.now();\n"
                        "const delay = now - msg.payload.ts_sent;\n"
                        "return {\n"
                        "  topic: 'sim/stats/delay',\n"
                        "  payload: {\n"
                        f"    name: '{sub['name']}',\n"
                        f"    topic: '{topic}',\n"
                        "    delay: delay,\n"
                        "    seq_id: msg.payload.seq_id || null,\n"
                        "    ts_sent: msg.payload.ts_sent,\n"
                        "    ts_recv: now\n"
                        "  }\n"
                        "};"
                    ),
                    "outputs": 1,
                    "noerr": 0,
                    "initialize": "",
                    "finalize": "",
                    "libs": [],
                    "x": 300,
                    "y": y,
                    "wires": [[mqtt_out_id]]
                },
                {
                    "id": mqtt_out_id,
                    "type": "mqtt out",
                    "z": tab_id,
                    "name": f"{sub['name']} ➤ sim/stats/delay",
                    "topic": "",
                    "qos": "0",
                    "retain": False,
                    "broker": broker_config_id,
                    "x": 530,
                    "y": y,
                    "wires": []
                }
            ])
            y += 80

    # Deploy flows to Node-RED
    try:
        resp = requests.post(
            f'{NODE_RED_URL}/flows',
            headers={'Content-Type': 'application/json'},
            json=all_nodes,
            timeout=10  # Add timeout
        )
        if resp.status_code == 204:
            return jsonify(ok=True, warnings=pub_warnings + sub_warnings)
        else:
            return jsonify(error=f"Failed to deploy: {resp.text}"), 500
    except requests.RequestException as e:
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

def start_delay_collector(broker_host, broker_port, delay_deque):
    client = mqtt.Client(CallbackAPIVersion.VERSION2)
    client.on_message = lambda cl, userdata, msg: on_message(cl, msg, delay_deque)
    try:
        client.connect(broker_host, broker_port, 60)
        client.subscribe("sim/stats/delay")
        client.loop_start()
        return client
    except Exception as e:
        print(f"❌ Failed to connect to broker: {e}")
        return None

def on_message(client, msg, delay_deque):
    try:
        payload = json.loads(msg.payload.decode())
        payload['timestamp'] = time.time()
        delay_deque.append(payload)
    except Exception as e:
        print(f"[Delay Parser Error] {e}")

# Start delay collector in background thread
# Thread(target=start_delay_collector, args=('localhost', 1883), daemon=True).start()

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
    broker_name = args.get('broker_name', 'localhost').lower()
    broker_port = int(args.get('broker_port', 1883))
    # Determine MQTT connection host
    mqtt_host = 'localhost' if broker_name in get_docker_broker_names() else broker_name

    duration = int(args.get('duration', 60))

    # Verify container exists
    container = get_broker_container(broker_name)
    if not container:
        job_status[job_id] = {
            'error': f'Broker container not found: {broker_name}',
            'status': 'failed'
        }
        return

    # Set up resource monitoring
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
    try:
        eval_results = controller.run()
    except Exception as e:
        stop_event.set()
        monitor_thread.join(timeout=5)
        job_status[job_id] = {
            'status': 'failed',
            'error': f'Evaluation error: {str(e)}'
        }
        return

    # Finish resource monitoring
    stop_event.set()
    monitor_thread.join(timeout=5)

    # Save final job state
    job_status[job_id] = {
        **eval_results,
        'status': 'done',
        'monitoring': 'done',
        'broker_name': broker_name,
        'job_id': job_id,
        'resource_csv': resource_csv
    }

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
    resource_csv = os.path.join('results', f'resource_usage_{broker_name}_{job_id}.csv')
    
    resource_data = []
    if os.path.exists(resource_csv):
        with open(resource_csv) as f:
            resource_data = list(csv.DictReader(f))
    
    return render_template ("results.html",
        broker_name=broker_name,
        job_id=job_id,
        stats=stats,
        resource_data=json.dumps(resource_data))

# =============================================================================
# MAIN ROUTES
# =============================================================================

@app.route('/')
def index():
    """Main application page"""
    return render_template('index.html')

if __name__ == '__main__':
    # Ensure results directory exists
    os.makedirs('results', exist_ok=True)
    app.run(debug=True)