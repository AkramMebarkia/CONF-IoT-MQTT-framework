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

# Multi-instance support
NODE_RED_INSTANCES_STR = os.getenv('NODE_RED_INSTANCES', '')
if NODE_RED_INSTANCES_STR:
    NODE_RED_INSTANCES = [url.strip() for url in NODE_RED_INSTANCES_STR.split(',') if url.strip()]
else:
    NODE_RED_INSTANCES = [NODE_RED_URL]

logger.info("Node-RED instances configured: %d", len(NODE_RED_INSTANCES))


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
# BROKER MANAGEMENT ROUTES
# =============================================================================

BROKERS_CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'brokers_config.json')
DOCKER_NETWORK_NAME = 'mqtt_benchmark_net'

def load_brokers_config():
    try:
        with open(BROKERS_CONFIG_PATH, 'r') as f:
            return json.load(f)
    except Exception as e:
        logger.error("Failed to load brokers config: %s", e)
        return {"brokers": []}

def ensure_network_exists():
    """Ensure the Docker network exists for broker/Node-RED communication"""
    try:
        client = docker.from_env()
        try:
            client.networks.get(DOCKER_NETWORK_NAME)
        except docker.errors.NotFound:
            client.networks.create(DOCKER_NETWORK_NAME, driver='bridge')
            logger.info("Created Docker network: %s", DOCKER_NETWORK_NAME)
    except Exception as e:
        logger.error("Failed to create network: %s", e)

@app.route('/api/brokers', methods=['GET'])
def list_brokers():
    """List all available brokers with their status"""
    config = load_brokers_config()
    brokers = []
    
    try:
        client = docker.from_env()
        for broker in config.get('brokers', []):
            status = 'stopped'
            try:
                container = client.containers.get(broker['container_name'])
                status = container.status
            except docker.errors.NotFound:
                status = 'not_created'
            except Exception:
                status = 'unknown'
            
            brokers.append({
                'name': broker['name'],
                'display_name': broker['display_name'],
                'description': broker.get('description', ''),
                'port': broker.get('port', 1883),
                'status': status
            })
    except Exception as e:
        logger.error("Error listing brokers: %s", e)
        return jsonify(error=str(e)), 500
    
    return jsonify(brokers=brokers)

@app.route('/api/brokers/<broker_name>/start', methods=['POST'])
def start_broker(broker_name):
    """Start a specific broker container, stopping any other running brokers first"""
    config = load_brokers_config()
    broker_config = None
    
    for b in config.get('brokers', []):
        if b['name'] == broker_name:
            broker_config = b
            break
    
    if not broker_config:
        return jsonify(error=f"Broker '{broker_name}' not found in config"), 404
    
    try:
        client = docker.from_env()
        ensure_network_exists()
        
        # Stop all other brokers first (they share port 1883)
        for b in config.get('brokers', []):
            if b['name'] != broker_name:
                try:
                    container = client.containers.get(b['container_name'])
                    if container.status == 'running':
                        container.stop()
                        logger.info("Stopped broker: %s", b['name'])
                except docker.errors.NotFound:
                    pass
        
        # Check if container exists
        try:
            container = client.containers.get(broker_config['container_name'])
            if container.status != 'running':
                container.start()
                logger.info("Started existing container: %s", broker_name)
            
            # Ensure container is on the correct network
            try:
                network = client.networks.get(DOCKER_NETWORK_NAME)
                if container.id not in [c.id for c in network.containers]:
                    network.connect(container)
                    logger.info("Connected %s to network %s", broker_name, DOCKER_NETWORK_NAME)
            except Exception as net_err:
                logger.warning("Network connection issue: %s", net_err)
            
            return jsonify(ok=True, message=f"Broker {broker_name} started", status='running')
        except docker.errors.NotFound:
            pass
        
        # Create and start new container
        ports = {f"{broker_config['port']}/tcp": broker_config['port']}
        if 'extra_ports' in broker_config:
            for p in broker_config['extra_ports']:
                ports[f"{p}/tcp"] = p
        
        env_vars = broker_config.get('environment', {})
        volumes = {}
        
        if 'config_volume' in broker_config:
            src, dst = broker_config['config_volume'].split(':')
            src = os.path.abspath(src)
            volumes[src] = {'bind': dst, 'mode': 'ro'}
        
        container = client.containers.run(
            broker_config['image'],
            name=broker_config['container_name'],
            ports=ports,
            environment=env_vars,
            volumes=volumes,
            network=DOCKER_NETWORK_NAME,
            detach=True
        )
        
        logger.info("Created and started broker: %s", broker_name)
        return jsonify(ok=True, message=f"Broker {broker_name} created and started", status='running')
        
    except Exception as e:
        logger.error("Failed to start broker %s: %s", broker_name, e)
        return jsonify(error=str(e)), 500

@app.route('/api/brokers/<broker_name>/stop', methods=['POST'])
def stop_broker(broker_name):
    """Stop a specific broker container"""
    try:
        client = docker.from_env()
        container = client.containers.get(broker_name)
        container.stop()
        logger.info("Stopped broker: %s", broker_name)
        return jsonify(ok=True, message=f"Broker {broker_name} stopped")
    except docker.errors.NotFound:
        return jsonify(error=f"Broker container '{broker_name}' not found"), 404
    except Exception as e:
        logger.error("Failed to stop broker: %s", e)
        return jsonify(error=str(e)), 500


# =============================================================================
# SIMULATION DEPLOYMENT ROUTES
# =============================================================================

@app.route('/deploy_simulation', methods=['POST'])
def deploy_simulation():
    """Deploy simulation flows to multiple Node-RED instances"""
    data = request.get_json()

    publisher_groups = data.get("publisher_groups", [])
    subscriber_groups = data.get("subscriber_groups", []) 

    broker_host = data.get("broker_name", "localhost")
    broker_port = int(data.get("broker_port", 1883))

    pub_expander = GroupExpander(mode="publisher")
    pub_instances, pub_warnings = pub_expander.expand(publisher_groups)

    sub_expander = GroupExpander(mode="subscriber")
    sub_instances, sub_warnings = sub_expander.expand(subscriber_groups)

    num_instances = len(NODE_RED_INSTANCES)
    logger.info("Distributing workload across %d Node-RED instances", num_instances)

    # Distribute publishers and subscribers round-robin
    instance_pubs = [[] for _ in range(num_instances)]
    instance_subs = [[] for _ in range(num_instances)]

    for i, pub in enumerate(pub_instances):
        instance_pubs[i % num_instances].append(pub)

    for i, sub in enumerate(sub_instances):
        instance_subs[i % num_instances].append(sub)

    deployment_results = []

    for idx, instance_url in enumerate(NODE_RED_INSTANCES):
        pubs = instance_pubs[idx]
        subs = instance_subs[idx]

        if not pubs and not subs:
            logger.info("Instance %d (%s): No nodes to deploy", idx, instance_url)
            continue

        all_nodes = []
        tab_id = new_id()
        all_nodes.append({
            "id": tab_id,
            "type": "tab",
            "label": "Sim-AutoFlow",
            "disabled": False,
            "info": f"Instance {idx}"
        })

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

        y = 80

        for pub in pubs:
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

        for sub in subs:
            for topic in sub["topics"]:
                mqtt_in_id = new_id()
                stats_func_id = new_id()

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
                        "wires": [[stats_func_id]]
                    },
                    {
                        "id": stats_func_id,
                        "type": "function",
                        "z": tab_id,
                        "name": "RunningStats",
                        "func": """
// Ultra-efficient running statistics - NO HTTP during test
// Uses flow-level context for global aggregation
try {
    const d = JSON.parse(msg.payload);
    const now = Date.now();
    const delay = now - d.t;
    
    // Skip invalid delays
    if (delay < 0 || delay > 60000) {
        return null;
    }
    
    // Get or initialize running stats
    var stats = flow.get('latencyStats') || {
        count: 0,
        sum: 0,
        min: Infinity,
        max: 0
    };
    
    // Update running statistics - O(1) memory
    stats.count++;
    stats.sum += delay;
    stats.min = Math.min(stats.min, delay);
    stats.max = Math.max(stats.max, delay);
    
    flow.set('latencyStats', stats);
    
    // No output - no HTTP calls during test!
    return null;
    
} catch (e) {
    return null;
}
""",
                        "outputs": 1,
                        "noerr": 0,
                        "initialize": "",
                        "finalize": "",
                        "libs": [],
                        "x": 300,
                        "y": y,
                        "wires": [[]]  # No wires - no HTTP node!
                    }
                ])
                y += 60

        try:
            logger.info("Deploying %d nodes to instance %d (%s) - Pubs: %d, Subs: %d", 
                       len(all_nodes), idx, instance_url, len(pubs), len(subs))
            resp = requests.post(
                f'{instance_url}/flows',
                headers={'Content-Type': 'application/json'},
                json=all_nodes,
                timeout=30
            )
            if resp.status_code == 204:
                deployment_results.append({"instance": idx, "url": instance_url, "ok": True, "pubs": len(pubs), "subs": len(subs)})
            else:
                deployment_results.append({"instance": idx, "url": instance_url, "ok": False, "error": resp.text})
                logger.error("Instance %d deployment failed: %s", idx, resp.text)
        except requests.RequestException as e:
            deployment_results.append({"instance": idx, "url": instance_url, "ok": False, "error": str(e)})
            logger.error("Instance %d connection failed: %s", idx, str(e))

    successful = sum(1 for r in deployment_results if r.get("ok"))
    logger.info("Deployment complete: %d/%d instances successful", successful, len(deployment_results))

    return jsonify(
        ok=successful > 0,
        warnings=pub_warnings + sub_warnings,
        instances=deployment_results,
        total_publishers=len(pub_instances),
        total_subscribers=len(sub_instances)
    )



@app.route('/simulation/<action>', methods=['POST'])
def control_simulation(action):
    """Control simulation across all Node-RED instances"""
    if action not in ('start', 'stop'):
        return jsonify({"error": "Invalid action"}), 400

    results = []
    total_inject_count = 0

    for idx, instance_url in enumerate(NODE_RED_INSTANCES):
        try:
            flows_resp = requests.get(f'{instance_url}/flows', timeout=120)
            flows_resp.raise_for_status()
            flows = flows_resp.json()

            sim_tab_id = None
            for node in flows:
                if node.get("type") == "tab" and node.get("label") == "Sim-AutoFlow":
                    sim_tab_id = node["id"]
                    break
            
            if not sim_tab_id:
                results.append({"instance": idx, "ok": False, "error": "No simulation tab"})
                continue

            inject_count = 0
            for node in flows:
                if node.get("type") == "inject" and node.get("z") == sim_tab_id:
                    if action == "stop":
                        node["repeat"] = ""
                        node["once"] = False
                        node["onceDelay"] = 0.1
                        node["crontab"] = ""
                    inject_count += 1

            resp = requests.post(
                f'{instance_url}/flows',
                headers={'Content-Type': 'application/json', 'Node-RED-Deployment-Type': 'full'},
                json=flows,
                timeout=30
            )
            
            if resp.status_code == 204:
                results.append({"instance": idx, "ok": True, "inject_count": inject_count})
                total_inject_count += inject_count
            else:
                results.append({"instance": idx, "ok": False, "error": resp.text})

        except requests.RequestException as e:
            results.append({"instance": idx, "ok": False, "error": str(e)})

    successful = sum(1 for r in results if r.get("ok"))
    logger.info("Simulation %s: %d/%d instances, %d inject nodes", action, successful, len(results), total_inject_count)
    
    return jsonify(ok=successful > 0, action=action, results=results, total_inject_nodes=total_inject_count)


def cleanup_simulation():
    """Remove simulation flows from all Node-RED instances"""
    success_count = 0
    
    for idx, instance_url in enumerate(NODE_RED_INSTANCES):
        try:
            flows_resp = requests.get(f'{instance_url}/flows', timeout=120)
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
                    f'{instance_url}/flows',
                    headers={'Content-Type': 'application/json', 'Node-RED-Deployment-Type': 'full'},
                    json=flows,
                    timeout=30
                )
                
                if resp.status_code == 204:
                    success_count += 1
                    logger.info("Instance %d cleanup successful", idx)
        except Exception as e:
            logger.error("Instance %d cleanup failed: %s", idx, e)
    
    logger.info("Cleanup complete: %d/%d instances", success_count, len(NODE_RED_INSTANCES))
    return success_count > 0


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


def collect_nodered_stats():
    """Fetch running latency stats from all Node-RED instances"""
    aggregated_stats = {
        'count': 0,
        'sum': 0,
        'min': float('inf'),
        'max': 0
    }
    
    for instance_url in NODE_RED_INSTANCES:
        try:
            # Use Node-RED's context/flow endpoint to get stats
            # First, we need to inject a trigger to get the stats
            flows_resp = requests.get(f'{instance_url}/flows', timeout=10)
            if flows_resp.status_code != 200:
                continue
                
            flows = flows_resp.json()
            
            # Find the Sim-AutoFlow tab
            tab_id = None
            for node in flows:
                if node.get('type') == 'tab' and node.get('label') == 'Sim-AutoFlow':
                    tab_id = node['id']
                    break
            
            if not tab_id:
                continue
            
            # Use Node-RED's context API to get flow context
            # The stats are stored at flow.latencyStats
            context_resp = requests.get(
                f'{instance_url}/flow/{tab_id}/context',
                timeout=10
            )
            
            if context_resp.status_code == 200:
                context = context_resp.json()
                stats = context.get('latencyStats', {})
                
                if stats.get('count', 0) > 0:
                    aggregated_stats['count'] += stats['count']
                    aggregated_stats['sum'] += stats['sum']
                    aggregated_stats['min'] = min(aggregated_stats['min'], stats['min'])
                    aggregated_stats['max'] = max(aggregated_stats['max'], stats['max'])
                    
                    logger.info("Collected stats from %s: count=%d, avg=%.2fms",
                               instance_url, stats['count'], stats['sum']/stats['count'])
        except Exception as e:
            logger.warning("Failed to collect stats from %s: %s", instance_url, e)
    
    # Calculate average
    if aggregated_stats['count'] > 0:
        aggregated_stats['avg'] = aggregated_stats['sum'] / aggregated_stats['count']
    else:
        aggregated_stats['avg'] = 0
        aggregated_stats['min'] = 0
    
    return aggregated_stats


@app.route('/api/collect_stats', methods=['POST'])
def api_collect_stats():
    """API endpoint to collect and return running stats from Node-RED"""
    stats = collect_nodered_stats()
    
    # Also add to delay_data for compatibility with existing evaluation
    if stats['count'] > 0:
        delay_data.clear()
        # Create synthetic records for the evaluation controller
        for _ in range(min(stats['count'], 1000)):  # Cap at 1000 for memory
            delay_data.append({
                'subscriber': 'aggregated',
                'topic': 'aggregated',
                'delay': stats['avg'],  # Use average for compatibility
                'timestamp': time.time()
            })
    
    return jsonify({
        'ok': True,
        'stats': stats,
        'message': f"Collected {stats['count']} samples, avg={stats.get('avg', 0):.2f}ms"
    })


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
        
        # Collect running stats from Node-RED (new optimized approach)
        nodered_stats = collect_nodered_stats()
        if nodered_stats['count'] > 0:
            eval_results['nodered_stats'] = nodered_stats
            logger.info("Collected Node-RED running stats: count=%d, avg=%.2fms, min=%.2fms, max=%.2fms",
                       nodered_stats['count'], nodered_stats['avg'],
                       nodered_stats['min'], nodered_stats['max'])
        
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
        resp = requests.get(f'{NODE_RED_URL}/flows', timeout=120)
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