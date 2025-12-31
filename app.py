import csv
import json
import logging
import os
import threading
import time
import uuid
from collections import deque
from datetime import datetime

import docker
import paho.mqtt.client as mqtt
import requests
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify
from paho.mqtt.enums import CallbackAPIVersion

from deployment.topic_manager import TopicManager
from deployment.group_expander import GroupExpander
from evaluation.controller import EvaluationController

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder="frontend/templates", static_folder="frontend/static")

delay_data = deque(maxlen=20000)
job_status = {}

NODE_RED_URL = os.getenv('NODE_RED_URL', 'http://localhost:1880')
BATCH_SIZE = int(os.getenv('BATCH_SIZE', '500'))
LATENCY_CALLBACK_URL = os.getenv('LATENCY_CALLBACK_URL', 'http://host.docker.internal:5000/api/latency_batch')


def get_broker_container(broker_name):
    try:
        client = docker.from_env()
        return client.containers.get(broker_name)
    except docker.errors.NotFound:
        return None
    except Exception as e:
        logger.error("Docker error: %s", e)
        return None

topic_manager = TopicManager()

def new_id():
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
    
    publisher_group_map = {}
    for pub in pub_instances:
        group = pub.get("group", "default")
        if group not in publisher_group_map:
            publisher_group_map[group] = []
        publisher_group_map[group].append(pub)
    
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
var pubName = '{pub['name']}';
var topic = '{pub['topic']}';
var payloadSize = {pub.get('payload_size', 256)};

if (!global.get('seq')) {{
    global.set('seq', {{}});
}}
if (!global.get('seq')[pubName]) {{
    global.get('seq')[pubName] = 0;
}}

global.get('seq')[pubName]++;
var seqId = global.get('seq')[pubName];

var payload = {{
    t: Date.now(),
    s: seqId,
    p: pubName
}};

if (payloadSize > 50) {{
    payload.d = 'X'.repeat(payloadSize - 50);
}}

return {{
    topic: topic,
    payload: JSON.stringify(payload),
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

    for sub in sub_instances:
        for topic in sub["topics"]:
            mqtt_in_id = new_id()
            delay_func_id = new_id()
            http_req_id = new_id()

            all_nodes.extend([
                {
                    "id": mqtt_in_id,
                    "type": "mqtt in",
                    "z": tab_id,
                    "name": f"{sub['name']} ← {topic}",
                    "topic": topic,
                    "qos": str(sub.get("qos", 1)),
                    "datatype": "auto",
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
const N = {BATCH_SIZE};
const bufKey = '{sub['name']}_latBuf';
let buf = context.get(bufKey) || [];

try {{
    const d = JSON.parse(msg.payload);
    const now = Date.now();
    const latency = now - d.t;

    if (latency < 0 || latency > 60000) {{
        return null;
    }}

    buf.push({{
        subscriber: '{sub['name']}',
        topic: msg.topic,
        delay: latency,
        publisher_name: d.p,
        seq_id: d.s,
        timestamp: now
    }});
    context.set(bufKey, buf);

    if (buf.length >= N) {{
        msg.url = '{LATENCY_CALLBACK_URL}';
        msg.method = 'POST';
        msg.headers = {{ 'Content-Type': 'application/json' }};
        msg.payload = buf;
        context.set(bufKey, []);
        return msg;
    }}

    return null;

}} catch (e) {{
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
                    "name": "Send Latency",
                    "method": "POST",
                    "ret": "txt",
                    "paytoqs": "ignore",
                    "url": "",
                    "tls": "",
                    "persist": False,
                    "proxy": "",
                    "insecureHTTPParser": False,
                    "authType": "",
                    "senderr": False,
                    "headers": [],
                    "x": 500,
                    "y": y,
                    "wires": [[]]
                }
            ])
            y += 80

    try:
        logger.info("Deploying %d nodes to Node-RED...", len(all_nodes))
        resp = requests.post(
            f'{NODE_RED_URL}/flows',
            headers={'Content-Type': 'application/json'},
            json=all_nodes,
            timeout=30
        )
        if resp.status_code == 204:
            logger.info("Successfully deployed to Node-RED - Publishers: %d, Subscribers: %d", 
                       len(pub_instances), len(sub_instances))
            return jsonify(ok=True, warnings=pub_warnings + sub_warnings)
        else:
            logger.error("Node-RED deployment failed: %d - %s", resp.status_code, resp.text)
            return jsonify(error=f"Failed to deploy: {resp.text}"), 500
    except requests.RequestException as e:
        logger.error("Node-RED connection failed: %s", str(e))
        return jsonify(error=f"Node-RED connection failed: {str(e)}"), 500



@app.route('/simulation/<action>', methods=['POST'])
def control_simulation(action):
    if action not in ('start', 'stop'):
        return jsonify({"error": "Invalid action"}), 400

    try:
        flows_resp = requests.get(f'{NODE_RED_URL}/flows', timeout=10)
        flows_resp.raise_for_status()
        flows = flows_resp.json()

        sim_tab_id = None
        for node in flows:
            if node.get("type") == "tab" and node.get("label") == "Sim-AutoFlow":
                sim_tab_id = node["id"]
                break
        
        if not sim_tab_id:
            return jsonify({"error": "Simulation tab not found"}), 404

        inject_count = 0
        for node in flows:
            if node.get("type") == "inject" and node.get("z") == sim_tab_id:
                if action == "stop":
                    node["repeat"] = ""
                    node["once"] = False
                    node["onceDelay"] = 0.1
                    node["crontab"] = ""
                elif action == "start":
                    pass
                inject_count += 1

        resp = requests.post(
            f'{NODE_RED_URL}/flows',
            headers={'Content-Type': 'application/json', 'Node-RED-Deployment-Type': 'full'},
            json=flows,
            timeout=30
        )
        
        if resp.status_code == 204:
            logger.info("Simulation %s: affected %d inject nodes (full redeploy)", action, inject_count)
            return jsonify(ok=True, action=action, inject_nodes_affected=inject_count)
        else:
            return jsonify(error=f"Failed to {action}: {resp.text}"), 500

    except requests.RequestException as e:
        return jsonify(error=f"Node-RED operation failed: {str(e)}"), 500


def cleanup_simulation():
    try:
        flows_resp = requests.get(f'{NODE_RED_URL}/flows', timeout=10)
        flows_resp.raise_for_status()
        flows = flows_resp.json()
        
        sim_tab_id = None
        for node in flows:
            if node.get("type") == "tab" and node.get("label") == "Sim-AutoFlow":
                sim_tab_id = node["id"]
                break
        
        if sim_tab_id:
            flows = [node for node in flows if node.get("z") != sim_tab_id and node.get("id") != sim_tab_id]
            
            resp = requests.post(
                f'{NODE_RED_URL}/flows',
                headers={'Content-Type': 'application/json', 'Node-RED-Deployment-Type': 'full'},
                json=flows,
                timeout=10
            )
            
            if resp.status_code == 204:
                logger.info("Simulation flows removed successfully")
                return True
    except Exception as e:
        logger.error("Failed to clean up simulation: %s", e)
    return False


@app.route('/api/metrics')
def get_delay_metrics():
    return jsonify(list(delay_data)[-100:])


@app.route('/api/latency', methods=['POST'])
def receive_latency():
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
        logger.error("Latency receiver error: %s", e)
        return jsonify(error=str(e)), 400


@app.route('/api/latency_batch', methods=['POST'])
def receive_latency_batch():
    try:
        batch = request.get_json(force=True)
        if not isinstance(batch, list):
            return jsonify(error="Expected JSON array"), 400

        now_ts = time.time()
        for rec in batch:
            delay_record = {
                'subscriber': rec.get('subscriber', 'unknown'),
                'topic': rec.get('topic', 'unknown'),
                'delay': float(rec.get('delay', 0)),
                'publisher_name': rec.get('publisher_name', 'unknown'),
                'seq_id': rec.get('seq_id'),
                'timestamp': rec.get('timestamp') or now_ts
            }
            delay_data.append(delay_record)
        
        if len(batch) > 0:
            logger.debug("Received %d latency samples", len(batch))
        
        return jsonify(ok=True, received=len(batch))
    except Exception as e:
        logger.error("Latency batch receiver error: %s", e)
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


def get_docker_broker_names():
    return {'activemq', 'mosquitto', 'vernemq', 'emqx', 'hivemq', 'nanomq', 'rabbitmq'}


def run_tests_in_background(job_id, args):
    try:
        broker_name = args.get('broker_name', 'localhost').lower()
        broker_port = int(args.get('broker_port', 1883))
        
        mqtt_host = 'localhost' if broker_name in get_docker_broker_names() else broker_name
        duration = int(args.get('duration', 60))

        logger.info("Starting evaluation for %s (job: %s)", broker_name, job_id)
        logger.info("Host: %s:%d, Duration: %ds", mqtt_host, broker_port, duration)

        container = None
        if broker_name in get_docker_broker_names():
            container = get_broker_container(broker_name)
            if not container:
                job_status[job_id] = {
                    'error': f'Broker container not found: {broker_name}',
                    'status': 'failed'
                }
                return

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

        controller = EvaluationController(
            broker_host=mqtt_host,
            broker_port=broker_port,
            duration=duration,
            job_id=job_id,
            delay_queue=delay_data,
            warmup_seconds=int(args.get('warmup', 10))
        )
        
        eval_results = controller.run()
        
        if 'error' in eval_results:
            raise Exception(eval_results['error'])

        if stop_event and monitor_thread:
            stop_event.set()
            monitor_thread.join(timeout=10)

        job_status[job_id] = {
            **eval_results,
            'status': 'done',
            'monitoring': 'done' if container else 'skipped',
            'broker_name': broker_name,
            'job_id': job_id,
            'resource_csv': resource_csv
        }
        
        logger.info("Evaluation completed for %s", broker_name)

        try:
            stop_resp = requests.post('http://localhost:5000/simulation/stop', timeout=10)
            if stop_resp.status_code == 200:
                logger.info("Simulation stopped successfully")
        except Exception as e:
            logger.warning("Failed to stop simulation cleanly: %s", e)

        time.sleep(5)

        try:
            cleanup_simulation()
        except Exception as e:
            logger.error("Cleanup error: %s", e)

        logger.info("Job %s finished and cleaned up", job_id)
        
    except Exception as e:
        logger.error("Test runner error: %s", e)
        
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


@app.route('/verify_flow', methods=['GET'])
def verify_flow():
    try:
        resp = requests.get(f'{NODE_RED_URL}/flows', timeout=5)
        flows = resp.json()
        
        node_types = {}
        for node in flows:
            node_type = node.get('type', 'unknown')
            node_types[node_type] = node_types.get(node_type, 0) + 1
        
        recent_delays = list(delay_data)[-10:] if delay_data else []
        
        return jsonify({
            "node_red_connected": resp.status_code == 200,
            "total_nodes": len(flows),
            "node_types": node_types,
            "recent_delay_count": len(delay_data),
            "recent_delays": recent_delays
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/health')
def health():
    return jsonify({
        "status": "healthy",
        "active_jobs": len([j for j in job_status.values() if j.get('status') == 'running'])
    })


@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(500)
def internal_error(error):
    return jsonify({"error": "Internal server error"}), 500


if __name__ == '__main__':
    os.makedirs('results', exist_ok=True)
    
    logger.info("Checking MQTT broker connectivity...")
    test_client = mqtt.Client(
        callback_api_version=CallbackAPIVersion.VERSION2,
        client_id="test_connection"
    )
    try:
        test_client.connect('localhost', 1883, 60)
        test_client.disconnect()
        logger.info("MQTT broker is accessible")
    except Exception as e:
        logger.warning("Cannot connect to MQTT broker: %s", e)
        logger.warning("Make sure your MQTT broker is running on localhost:1883")
    
    logger.info("Starting Flask app at http://localhost:5000")
    app.run(debug=True, host='0.0.0.0', port=5000, use_reloader=False)