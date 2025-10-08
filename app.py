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
app = Flask(__name__, template_folder="frontend/templates", static_folder="frontend/static")

# Global variables
delay_data = deque(maxlen=20000)
delay_collector_client = None
job_status = {}

# Configuration constants
NODE_RED_URL = 'http://localhost:1880'

# Number of latency records to sends to the flask app
BATCH_SIZE = 100



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

@app.route('/deploy_simulation', methods=['POST'])
def deploy_simulation():
    """Deploy simulation flows to Node-RED"""
    data = request.get_json()

    publisher_groups = data.get("publisher_groups", [])
    subscriber_groups = data.get("subscriber_groups", []) 

    # Get broker configuration
    broker_host = data.get("broker_name", "localhost")
    broker_port = int(data.get("broker_port", 1883))

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
    
    # Group publishers by their original group for shared sequence counters
    publisher_groups = {}
    for pub in pub_instances:
        group = pub.get("group", "default")
        if group not in publisher_groups:
            publisher_groups[group] = []
        publisher_groups[group].append(pub)
    
    for pub in pub_instances:
        inject_id = new_id()
        function_id = new_id()
        mqtt_id = new_id()

        all_nodes.extend([
            {
                "id": inject_id,
                "type": "inject",
                "z": tab_id,
                "name": f"{pub['name']} Timer",
                "props": [{"p": "payload"}],
                "repeat": str(pub.get("interval", 1.0)), 
                "crontab": "",
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
                "name": f"{pub['name']} Generator",
                "func": f"""
// Enhanced message generation with metadata header
var pubName = '{pub['name']}';
var topic = '{pub['topic']}';
var payloadSize = {pub.get('payload_size', 256)};

// Initialize sequence counter
if (!global.get('seq')) {{
    global.set('seq', {{}});
}}
if (!global.get('seq')[pubName]) {{
    global.get('seq')[pubName] = 0;
}}

global.get('seq')[pubName]++;
var seqId = global.get('seq')[pubName];

// --- Create binary buffer ---
// Format: [8 bytes timestamp][4 bytes seq_id][1 byte name_length][publisher_name][payload...]
var pubNameBuf = Buffer.from(pubName, 'utf8');
var headerSize = 8 + 4 + 1 + pubNameBuf.length;
var buffer = Buffer.alloc(Math.max(headerSize, payloadSize));

var now = BigInt(Date.now());
buffer.writeBigInt64BE(now, 0);
buffer.writeUInt32BE(seqId, 8);
buffer.writeUInt8(pubNameBuf.length, 12);
pubNameBuf.copy(buffer, 13);

// Fill the rest with data
if (payloadSize > headerSize) {{
    buffer.fill('X', headerSize);
}}

// Optional: log every 10th message
if (seqId % 10 === 0) {{
    node.log(pubName + ' sent message #' + seqId + ' to ' + topic);
}}

return {{
    topic: topic,
    payload: buffer,
    qos: {pub.get('qos', 1)},
    retain: {str(pub.get('retain', False)).lower()},
}};
""",
                "outputs": 1,
                "noerr": 0,
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

# Updated subscriber node creation for deploy_simulation function
# Replace the subscriber section in your app.py with this:

    # Add subscriber nodes (starting at line ~325 in app.py)
# Add subscriber nodes (complete with HTTP POST stage)
    for sub in sub_instances:
        for topic in sub["topics"]:
            mqtt_in_id = new_id()
            delay_func_id = new_id()
            http_req_id = new_id()  # NEW: HTTP request node

            all_nodes.extend([
                {
                    "id": mqtt_in_id,
                    "type": "mqtt in",
                    "z": tab_id,
                    "name": f"{sub['name']} ← {topic}",
                    "topic": topic,
                    "qos": str(sub.get("qos", 1)),
                    "datatype": "buffer",  # receive binary payload
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
                    "func": f"""
            // --- N-sample latency batching ---
            const N = {BATCH_SIZE};
            const bufKey = '{sub['name']}_latBuf';
            let buf = context.get(bufKey) || [];
            let count = context.get('count') || 0;

            try {{
                const b = Buffer.isBuffer(msg.payload) ? msg.payload : Buffer.from(msg.payload);
                const ts_sent = Number(b.readBigInt64BE(0));
                const now = Date.now();
                const latency = now - ts_sent;

                if (latency < 0 || latency > 60000) {{
                    node.warn(`Invalid delay ${{latency}}ms`);
                    return null;
                }}

                count++;
                if (count % 50 === 0)
                    node.log(`[${{count}}] {sub['name']} got delay ${{latency}}ms for ${{msg.topic}}`);
                context.set('count', count);

                buf.push({{
                    subscriber: '{sub['name']}',
                    topic: msg.topic,
                    delay: latency,
                    timestamp: now
                }});
                context.set(bufKey, buf);

                if (buf.length >= N) {{
                    msg.url = 'http://host.docker.internal:5000/api/latency_batch';
                    msg.method = 'POST';
                    msg.headers = {{ 'Content-Type': 'application/json' }};
                    msg.payload = buf;
                    context.set(bufKey, []);
                    return msg;  // Forward to HTTP request node
                }}
                return null;
            }} catch (e) {{
                node.error('Latency calc failed: ' + e.message);
                return null;
            }}
            """,
                    "outputs": 1,
                    "noerr": 0,
                    "initialize": "",
                    "finalize": "",
                    "libs": [],
                    "x": 300,
                    "y": y,
                    "wires": [[http_req_id]]
                },
                {
                    "id": http_req_id,
                    "type": "http request",
                    "z": tab_id,
                    "name": "Post to Flask",
                    "method": "use",
                    "ret": "txt",
                    "paytoqs": "ignore",
                    "url": "",  # taken from msg.url
                    "tls": "",
                    "persist": False,
                    "proxy": "",
                    "authType": "",
                    "x": 520,
                    "y": y,
                    "wires": [[]]
                }
            ])
            y += 80


    # Deploy flows to Node-RED
    try:
        print(f"Deploying {len(all_nodes)} nodes to Node-RED...")
        resp = requests.post(
            f'{NODE_RED_URL}/flows',
            headers={'Content-Type': 'application/json'},
            json=all_nodes,
            timeout=30
        )
        if resp.status_code == 204:
            print("Successfully deployed to Node-RED")
            print(f"   Publishers: {len(pub_instances)}")
            print(f"   Subscribers: {len(sub_instances)}")
            return jsonify(ok=True, warnings=pub_warnings + sub_warnings)
        else:
            print(f"Node-RED deployment failed: {resp.status_code} - {resp.text}")
            return jsonify(error=f"Failed to deploy: {resp.text}"), 500
    except requests.RequestException as e:
        print(f"Node-RED connection failed: {str(e)}")
        return jsonify(error=f"Node-RED connection failed: {str(e)}"), 500

# =============================================================================
# SIMULATION CONTROL ROUTES
# =============================================================================

@app.route('/simulation/<action>', methods=['POST'])
def control_simulation(action):
    """Start or stop the simulation by enabling/disabling inject nodes"""
    if action not in ('start', 'stop'):
        return jsonify({"error": "Invalid action"}), 400

    try:
        # Get all current flows
        flows_resp = requests.get(f'{NODE_RED_URL}/flows', timeout=10)
        flows_resp.raise_for_status()
        flows = flows_resp.json()

        # Find the simulation tab
        sim_tab_id = None
        for node in flows:
            if node.get("type") == "tab" and node.get("label") == "Sim-AutoFlow":
                sim_tab_id = node["id"]
                break
        
        if not sim_tab_id:
            return jsonify({"error": "Simulation tab not found"}), 404

        # Toggle inject nodes in the simulation tab
        inject_count = 0
        for node in flows:
            if node.get("type") == "inject" and node.get("z") == sim_tab_id:
                if action == "stop":
                    node["repeat"] = ""  # Clear repeat to stop injection
                    node["once"] = False  # Prevent re-triggering on deploy
                inject_count += 1

        # Redeploy with updated inject states
        resp = requests.post(
            f'{NODE_RED_URL}/flows',
            headers={'Content-Type': 'application/json', 'Node-RED-Deployment-Type': 'flows'},
            json=flows,
            timeout=10
        )
        
        if resp.status_code == 204:
            print(f"[SimControl] {action.capitalize()}ped {inject_count} inject nodes")
            return jsonify(ok=True, action=action, inject_nodes_affected=inject_count)
        else:
            return jsonify(error=f"Failed to {action}: {resp.text}"), 500

    except requests.RequestException as e:
        return jsonify(error=f"Node-RED operation failed: {str(e)}"), 500


def cleanup_simulation():
    """Clean up simulation by removing the Sim-AutoFlow tab"""
    try:
        # Get all flows
        flows_resp = requests.get(f'{NODE_RED_URL}/flows', timeout=10)
        flows_resp.raise_for_status()
        flows = flows_resp.json()
        
        # Find and remove simulation nodes
        sim_tab_id = None
        for node in flows:
            if node.get("type") == "tab" and node.get("label") == "Sim-AutoFlow":
                sim_tab_id = node["id"]
                break
        
        if sim_tab_id:
            # Remove all nodes associated with this tab
            flows = [node for node in flows if node.get("z") != sim_tab_id and node.get("id") != sim_tab_id]
            
            # Redeploy
            resp = requests.post(
                f'{NODE_RED_URL}/flows',
                headers={'Content-Type': 'application/json', 'Node-RED-Deployment-Type': 'full'},
                json=flows,
                timeout=10
            )
            
            if resp.status_code == 204:
                print("[Cleanup] Simulation flows removed successfully")
                return True
    except Exception as e:
        print(f"[Cleanup] Failed to clean up simulation: {e}")
    return False
# =============================================================================
# MQTT DELAY COLLECTION
# =============================================================================

# def start_delay_collector(broker_host, broker_port, delay_deque):
#     """Start MQTT client to collect delay measurements"""
#     # Use the new callback API version
#     client = mqtt.Client(
#         callback_api_version=CallbackAPIVersion.VERSION2,
#         client_id=f"flask_delay_collector_{uuid.uuid4().hex[:8]}",
#         protocol=mqtt.MQTTv311
#     )
    
#     # Store connection state
#     client.connected = False
#     client.reconnect_delay = 5
    
#     def on_connect(client, userdata, flags, reason_code, properties):
#             if reason_code == 0:
#                 print(f"Delay collector connected to {broker_host}:{broker_port}")
#                 client.connected = True
#                 result = client.subscribe("sim/stats/delay", qos=1)
#                 print("ubscribed to sim/stats/delay")
#             else:
#                 print(f"Delay collector connection failed: {reason_code}")
#                 client.connected = False
        
#     def on_message(client, userdata, msg):
#         try:
#             payload_str = msg.payload.decode() if isinstance(msg.payload, bytes) else str(msg.payload)
#             payload = json.loads(payload_str)
#             payload['timestamp'] = time.time()
#             delay_deque.append(payload)
#             publisher_name = payload.get('publisher_name', 'unknown')
#             print(f"Delay data received: {payload.get('delay', 'N/A')}ms from {publisher_name}")
#         except Exception as e:
#             print(f"Delay parser error: {e}, payload: {msg.payload}")
        
#     def on_disconnect(client, userdata, reason_code, properties):
#             print(f"[DelayCollector] Disconnected from broker (rc={reason_code})")
#             client.connected = False
#             if reason_code != 0:
#                 # Attempt reconnection after delay
#                 print(f"⏳ [DelayCollector] Will attempt reconnection in {client.reconnect_delay} seconds...")
#                 time.sleep(client.reconnect_delay)
#                 try:
#                     client.reconnect()
#                 except Exception as e:
#                     print(f"[DelayCollector] Reconnection failed: {e}")
        
#     client.on_connect = on_connect
#     client.on_message = on_message
#     client.on_disconnect = on_disconnect
        
#     # Enable automatic reconnection
#     client.reconnect_delay_set(min_delay=1, max_delay=120)
        
#     try:
#             print(f"Connecting delay collector to {broker_host}:{broker_port}")
#             client.connect(broker_host, broker_port, 60)
#             client.loop_start()
#             print(f"Delay collector loop started for {broker_host}:{broker_port}")
#             return client
#     except Exception as e:
#             print(f"Failed to connect delay collector: {e}")
#             return None

# # Global delay collector client
# delay_collector_client = None

@app.route('/api/metrics')
def get_delay_metrics():
        """Get latest delay metrics"""
        return jsonify(list(delay_data)[-100:])

@app.route('/api/latency', methods=['POST'])
def receive_latency():
    """Receive single latency measurement"""
    try:
        data = request.get_json(force=True)
        delay_data.append({
            'subscriber': data.get('subscriber', 'unknown'),
            'topic': data.get('topic', 'unknown'),
            'delay': float(data.get('delay', 0)),
            'timestamp': data.get('ts') or data.get('timestamp') or time.time()
        })
        return jsonify(ok=True)
    except Exception as e:
        print(f"[LatencyReceiver] Error: {e}")
        return jsonify(error=str(e)), 400



@app.route('/api/latency_batch', methods=['POST'])
def receive_latency_batch():
    """Receive a batch (array) of latency measurements with full metadata"""
    try:
        batch = request.get_json(force=True)
        if not isinstance(batch, list):
            return jsonify(error="Expected JSON array"), 400

        now_ts = time.time()
        for rec in batch:
            # Store complete record with all metadata
            delay_record = {
                'subscriber': rec.get('subscriber', 'unknown'),
                'topic': rec.get('topic', 'unknown'),
                'delay': float(rec.get('delay', 0)),
                'publisher_name': rec.get('publisher_name', 'unknown'),
                'seq_id': rec.get('seq_id'),
                'timestamp': rec.get('timestamp') or now_ts
            }
            delay_data.append(delay_record)
            
        # Log batch receipt
        if len(batch) > 0:
            print(f"[LatencyBatch] Received {len(batch)} samples. "
                  f"First: pub={batch[0].get('publisher_name')}, "
                  f"seq={batch[0].get('seq_id')}, "
                  f"delay={batch[0].get('delay')}ms")
        
        return jsonify(ok=True, received=len(batch))
    except Exception as e:
        print(f"[LatencyBatchReceiver] Error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify(error=str(e)), 400


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

            print(f"   [TestRunner] Starting evaluation for {broker_name} (job: {job_id})")
            print(f"   Host: {mqtt_host}:{broker_port}")
            print(f"   Duration: {duration}s")

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
                job_id=job_id,
                delay_queue=delay_data
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
            
                        # ---- Evaluation complete ----
            print(f"[TestRunner] Evaluation completed for {broker_name}")

            # 1️⃣ Stop the simulation flows gracefully
            print("[TestRunner] Stopping simulation flows...")
            try:
                stop_resp = requests.post('http://localhost:5000/simulation/stop', timeout=10)
                if stop_resp.status_code == 200:
                    print("[TestRunner] Simulation stopped successfully")
                else:
                    print(f"[TestRunner] Stop request returned {stop_resp.status_code}")
            except Exception as e:
                print(f"[TestRunner] Warning: failed to stop simulation cleanly: {e}")

            # 2️⃣ Allow Node-RED to flush remaining HTTP batches
            print("[TestRunner] Waiting 5 seconds for final latency posts...")
            time.sleep(5)

            # 3️⃣ Clean up Node-RED flows to free memory and CPU
            print("[TestRunner] Cleaning up simulation flows...")
            try:
                success = cleanup_simulation()
                if success:
                    print("[TestRunner] Cleanup complete – Sim-AutoFlow tab removed.")
                else:
                    print("[TestRunner] Cleanup skipped or failed.")
            except Exception as e:
                print(f"[TestRunner] Cleanup error: {e}")

            # ---- Done ----
            print(f"[TestRunner] Job {job_id} finished and cleaned up.")

            
        except Exception as e:
            print(f"[TestRunner] Error: {e}")
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

# =============================================================================
# MAIN ENTRY POINT
# =============================================================================

if __name__ == '__main__':
        # Ensure results directory exists
        os.makedirs('results', exist_ok=True)
        
        # Check if MQTT broker is running
        print("Checking MQTT broker connectivity...")
        test_client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id="test_connection"
        )
        try:
            test_client.connect('localhost', 1883, 60)
            test_client.disconnect()
            print("MQTT broker is accessible")
        except Exception as e:
            print(f"WARNING: Cannot connect to MQTT broker: {e}")
            print("   Make sure your MQTT broker is running on localhost:1883")
        
        # START THE DELAY COLLECTOR
        # print("Starting delay collector...")
        # delay_collector_client = start_delay_collector('localhost', 1883, delay_data)
        
        if delay_collector_client:
            print("Delay collector started successfully")
        else:
            print("WARNING: Delay collector failed to start!")
            print("The application will continue but delay metrics won't be collected")
        
        print("Starting Flask app...")
        print("Access the application at: http://localhost:5000")
        
        # Run Flask app
        app.run(debug=True, host='0.0.0.0', port=5000, use_reloader=False)