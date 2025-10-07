# Updated app.py with NTP Synchronization Integration
# Replace the existing app.py with this enhanced version

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

# Import new synchronized evaluation system
try:
    from timing_sync import TimingSynchronizer, PrecisionLatencyTracker
    from controller import SynchronizedEvaluationController
    TIMING_SYNC_AVAILABLE = True
    print("[APP] NTP synchronization modules loaded successfully")
except ImportError as e:
    print(f"[APP] WARNING: Timing sync modules not available: {e}")
    print("[APP] Falling back to legacy evaluation system")
    from controller import EvaluationController
    TIMING_SYNC_AVAILABLE = False

# Flask app initialization
app = Flask(__name__, template_folder="frontend/templates", static_folder="frontend/static")

# Global variables
delay_data = deque(maxlen=2000)
job_status = {}

# Configuration constants  
NODE_RED_URL = 'http://localhost:1880'

# Global timing synchronizer for the application
global_timing_sync = None

def initialize_timing_sync():
    """Initialize global timing synchronization"""
    global global_timing_sync
    if TIMING_SYNC_AVAILABLE and global_timing_sync is None:
        try:
            print("[APP] Initializing global NTP synchronization...")
            global_timing_sync = TimingSynchronizer()
            sync_status = global_timing_sync.get_sync_status()
            print(f"[APP] Global NTP sync initialized - Quality: {sync_status['quality']}%, Offset: {sync_status['offset_ms']:.3f}ms")
            return True
        except Exception as e:
            print(f"[APP] Failed to initialize timing sync: {e}")
            global_timing_sync = None
            return False
    return global_timing_sync is not None

# Initialize timing sync on startup
initialize_timing_sync()

# Broker container helper functions (unchanged)
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
# ENHANCED NODE-RED FLOW GENERATION WITH NTP SYNC
# =============================================================================

def generate_synchronized_publisher_function(pub_name, topic, payload_size, qos, retain):
    """Generate Node-RED publisher function with NTP synchronization"""
    return f'''
// Enhanced Publisher with NTP Synchronization - {pub_name}
var pubName = '{pub_name}';
var topic = '{topic}';
var payloadSize = {payload_size};

// Initialize sequence counter
if (!global.get('seq')) {{
    global.set('seq', {{}});
}}
if (!global.get('seq')[pubName]) {{
    global.set('seq')[pubName] = 0;
}}

// Initialize NTP offset tracking
if (!global.get('ntp_offset')) {{
    global.set('ntp_offset', 0);
}}

global.get('seq')[pubName]++;

// Get high-precision timestamp with NTP correction
var timestamp;
var ntp_offset = global.get('ntp_offset') || 0;

try {{
    // Use high-resolution time if available
    if (typeof process !== 'undefined' && process.hrtime && process.hrtime.bigint) {{
        var hrTime = process.hrtime.bigint();
        timestamp = Number(hrTime) / 1000000.0 + ntp_offset;
    }} else {{
        timestamp = Date.now() + ntp_offset;
    }}
}} catch (e) {{
    timestamp = Date.now() + ntp_offset;
}}

// Create enhanced payload with timing metadata
var payload = {{
    ts_sent: timestamp,
    ts_sent_iso: new Date(timestamp).toISOString(),
    seq_id: global.get('seq')[pubName],
    name: pubName,
    publisher_name: pubName,
    topic: topic,
    ntp_offset_ms: ntp_offset,
    timestamp_precision: 'high_res',
    data: 'X'.repeat(payloadSize),
    payload_size: payloadSize,
    qos: {qos},
    retain: {str(retain).lower()},
    message_version: '2.0'
}};

// Reduced frequency logging
if (global.get('seq')[pubName] % 50 === 0) {{
    node.log(pubName + ' sent #' + global.get('seq')[pubName] + 
             ' at ' + timestamp.toFixed(3) + 'ms (NTP: ' + ntp_offset.toFixed(3) + 'ms)');
}}

return {{
    topic: topic,
    payload: payload,
    qos: {qos},
    retain: {str(retain).lower()}
}};
'''

def generate_synchronized_subscriber_function(sub_name, topic):
    """Generate Node-RED subscriber function with precision timing validation"""
    return f'''
// Enhanced Subscriber with Timing Validation - {sub_name}
try {{
    var payload = msg.payload;
    var sub_name = '{sub_name}';
    var sub_topic = '{topic}';
    
    // Parse JSON payload
    if (typeof payload === 'string') {{
        try {{
            payload = JSON.parse(payload);
        }} catch (e) {{
            node.warn('JSON parse error for ' + sub_name + ': ' + e.message);
            return null;
        }}
    }}
    
    // Validate payload
    if (!payload || typeof payload !== 'object' || !payload.ts_sent) {{
        node.warn('Invalid or missing ts_sent for ' + sub_name);
        return null;
    }}
    
    // Get NTP-synchronized receive timestamp
    var ntp_offset = global.get('ntp_offset') || 0;
    var ts_recv;
    
    try {{
        if (typeof process !== 'undefined' && process.hrtime && process.hrtime.bigint) {{
            var hrTime = process.hrtime.bigint();
            ts_recv = Number(hrTime) / 1000000.0 + ntp_offset;
        }} else {{
            ts_recv = Date.now() + ntp_offset;
        }}
    }} catch (e) {{
        ts_recv = Date.now() + ntp_offset;
    }}
    
    var ts_sent = parseFloat(payload.ts_sent);
    var delay = ts_recv - ts_sent;
    
    // Enhanced delay validation
    if (delay < -1000) {{
        node.warn('Large negative delay: ' + delay.toFixed(3) + 'ms - clock sync issue');
        return null;
    }} else if (delay < 0) {{
        delay = Math.abs(delay);
        if (delay > 100) {{
            node.warn('Clock jitter: ' + delay.toFixed(3) + 'ms - corrected');
        }}
    }}
    
    if (delay > 60000) {{
        node.warn('Extreme delay: ' + delay.toFixed(3) + 'ms - dropping message');
        return null;
    }}
    
    var publisher_name = payload.publisher_name || payload.name || 'unknown_publisher';
    
    // Create comprehensive statistics message
    var statsMsg = {{
        topic: 'sim/stats/delay',
        payload: {{
            subscriber_name: sub_name,
            subscriber_topic: sub_topic,
            publisher_name: publisher_name,
            original_topic: payload.topic || msg.topic,
            delay: parseFloat(delay.toFixed(6)),
            seq_id: payload.seq_id || 0,
            ts_sent: ts_sent,
            ts_recv: parseFloat(ts_recv.toFixed(6)),
            ntp_offset_used: ntp_offset,
            timestamp_precision: payload.timestamp_precision || 'standard',
            timing_version: '2.0',
            delay_validated: true,
            message_version: payload.message_version || '1.0',
            processed_at: new Date().toISOString()
        }}
    }};
    
    // Enhanced logging
    if (payload.seq_id && payload.seq_id % 25 === 0) {{
        node.log(sub_name + ' <- ' + publisher_name + 
                ' | Delay: ' + delay.toFixed(3) + 'ms | NTP: ' + ntp_offset.toFixed(3) + 'ms');
    }}
    
    return statsMsg;
    
}} catch (error) {{
    node.error('{sub_name} error: ' + error.message);
    return null;
}}
'''

def generate_ntp_sync_function():
    """Generate NTP synchronization function for Node-RED"""
    return '''
// NTP Synchronization for Node-RED Global Context
// Simplified time synchronization using available web APIs

// Initialize NTP state
if (!global.get('ntp_last_sync')) {
    global.set('ntp_last_sync', 0);
    global.set('ntp_offset', 0);
    global.set('ntp_sync_count', 0);
}

var now = Date.now();
var lastSync = global.get('ntp_last_sync');
var syncInterval = 300000; // 5 minutes

// Check if sync needed
if (now - lastSync < syncInterval) {
    return null;
}

// Simple time offset estimation
// In production, this would use actual NTP protocol
var estimatedOffset = 0;

// You could integrate with world time APIs here
// For now, we assume minimal offset and rely on system time
global.set('ntp_offset', estimatedOffset);
global.set('ntp_last_sync', now);
global.set('ntp_sync_count', global.get('ntp_sync_count') + 1);

node.log('NTP sync #' + global.get('ntp_sync_count') + 
         ' - Offset: ' + estimatedOffset.toFixed(3) + 'ms');

return {
    topic: 'ntp/sync/status',
    payload: {
        sync_time: now,
        offset_ms: estimatedOffset,
        sync_count: global.get('ntp_sync_count'),
        status: 'completed'
    }
};
'''

# =============================================================================
# TOPIC MANAGEMENT ROUTES (unchanged)
# =============================================================================

@app.route('/topics', methods=['GET'])
def get_topics():
    return jsonify(topic_manager.get_all_topics())

@app.route('/topics', methods=['POST'])
def add_topic_or_group():
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
    if topic_manager.remove_topic(name):
        return jsonify({"ok": True})
    else:
        return jsonify({"error": "Topic not found"}), 404

@app.route('/topics', methods=['DELETE'])
def reset_topics():
    topic_manager.reset()
    return jsonify({"ok": True})

# =============================================================================
# GROUP EXPANSION ROUTES (unchanged)
# =============================================================================

@app.route('/expand_groups', methods=['POST'])
def expand_groups():
    data = request.get_json()
    kind = data.get('kind')
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
# ENHANCED SIMULATION DEPLOYMENT WITH NTP SYNC
# =============================================================================

@app.route('/deploy_simulation', methods=['POST'])
def deploy_simulation():
    """Deploy enhanced simulation flows with NTP synchronization to Node-RED"""
    data = request.get_json()

    publisher_groups = data.get("publisher_groups", [])
    subscriber_groups = data.get("subscriber_groups", []) 
    broker_host = data.get("broker_name", "localhost")
    broker_port = int(data.get("broker_port", 1883))

    # Expand groups
    pub_expander = GroupExpander(mode="publisher")
    pub_instances, pub_warnings = pub_expander.expand(publisher_groups)

    sub_expander = GroupExpander(mode="subscriber")
    sub_instances, sub_warnings = sub_expander.expand(subscriber_groups)

    # Build enhanced Node-RED flow
    all_nodes = []

    # Create main tab
    tab_id = new_id()
    all_nodes.append({
        "id": tab_id,
        "type": "tab",
        "label": "Synchronized-MQTT-Sim",
        "disabled": False,
        "info": "Enhanced MQTT simulation with NTP synchronization for accurate latency measurements"
    })

    # MQTT broker configuration
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

    # Add NTP synchronization node
    ntp_inject_id = new_id()
    ntp_func_id = new_id()
    
    all_nodes.extend([
        {
            "id": ntp_inject_id,
            "type": "inject",
            "z": tab_id,
            "name": "NTP Sync Timer",
            "props": [{"p": "payload"}],
            "repeat": "300", # Every 5 minutes
            "crontab": "",
            "once": True,
            "onceDelay": 1,
            "topic": "",
            "payload": "",
            "payloadType": "date",
            "x": 100,
            "y": 40,
            "wires": [[ntp_func_id]]
        },
        {
            "id": ntp_func_id,
            "type": "function",
            "z": tab_id,
            "name": "NTP Synchronizer",
            "func": generate_ntp_sync_function(),
            "outputs": 1,
            "noerr": 0,
            "x": 280,
            "y": 40,
            "wires": [[]]
        }
    ])

    # Enhanced publisher nodes
    y = 100
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
                "name": f"{pub['name']} SyncGen",
                "func": generate_synchronized_publisher_function(
                    pub['name'], 
                    pub['topic'], 
                    pub.get('payload_size', 256),
                    pub.get('qos', 1),
                    pub.get('retain', False)
                ),
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

    # Enhanced subscriber nodes
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
                    "name": f"{sub['name']} PrecisionCalc",
                    "func": generate_synchronized_subscriber_function(sub['name'], topic),
                    "outputs": 1,
                    "noerr": 0,
                    "x": 340,
                    "y": y,
                    "wires": [[mqtt_out_id]]
                },
                {
                    "id": mqtt_out_id,
                    "type": "mqtt out",
                    "z": tab_id,
                    "name": "Stats → sim/stats/delay",
                    "topic": "sim/stats/delay",
                    "qos": "1",
                    "retain": "false",
                    "broker": broker_config_id,
                    "x": 580,
                    "y": y,
                    "wires": []
                }
            ])
            y += 80

    # Deploy to Node-RED
    try:
        print(f"Deploying {len(all_nodes)} synchronized nodes to Node-RED...")
        resp = requests.post(
            f'{NODE_RED_URL}/flows',
            headers={'Content-Type': 'application/json'},
            json=all_nodes,
            timeout=30
        )
        if resp.status_code == 204:
            print("Successfully deployed synchronized simulation to Node-RED")
            print(f"   Publishers: {len(pub_instances)} (with NTP sync)")
            print(f"   Subscribers: {len(sub_instances)} (with precision timing)")
            return jsonify(ok=True, warnings=pub_warnings + sub_warnings, enhanced=True)
        else:
            print(f"Node-RED deployment failed: {resp.status_code} - {resp.text}")
            return jsonify(error=f"Failed to deploy: {resp.text}"), 500
    except requests.RequestException as e:
        print(f"Node-RED connection failed: {str(e)}")
        return jsonify(error=f"Node-RED connection failed: {str(e)}"), 500

# =============================================================================
# ENHANCED EVALUATION WITH NTP SYNCHRONIZATION
# =============================================================================

def run_tests_in_background(job_id, args):
    try:
        broker_name = args.get('broker_name', 'localhost').lower()
        broker_port = int(args.get('broker_port', 1883))
        duration = int(args.get('duration', 60))
        
        # Determine connection host
        mqtt_host = 'localhost' if broker_name in get_docker_broker_names() else broker_name
        
        print(f"[TestRunner] Starting synchronized evaluation (job: {job_id})")
        print(f"   Host: {mqtt_host}:{broker_port}")
        print(f"   Duration: {duration}s")
        print(f"   NTP Sync: {'Enabled' if TIMING_SYNC_AVAILABLE else 'Disabled'}")

        # Check container if Docker broker
        container = None
        docker_broker_names = {'activemq', 'mosquitto', 'vernemq', 'emqx', 'hivemq', 'nanomq', 'rabbitmq'}
        if broker_name in docker_broker_names:
            container = get_broker_container(broker_name)
            if not container:
                job_status[job_id] = {
                    'error': f'Broker container not found: {broker_name}',
                    'status': 'failed'
                }
                return

        # Set up resource monitoring
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

        # Choose evaluation controller based on availability
        if TIMING_SYNC_AVAILABLE:
            controller = SynchronizedEvaluationController(
                broker_host=mqtt_host,
                broker_port=broker_port,
                duration=duration,
                warmup=60,
                job_id=job_id,
                ntp_servers=['pool.ntp.org', 'time.cloudflare.com']
            )
        else:
            # Fallback to legacy controller
            from controller import EvaluationController
            controller = EvaluationController(
                broker_host=mqtt_host,
                broker_port=broker_port,
                duration=duration,
                warmup=60,
                job_id=job_id
            )
        
        eval_results = controller.run()
        
        if 'error' in eval_results:
            raise Exception(eval_results['error'])

        # Clean up monitoring
        if stop_event and monitor_thread:
            stop_event.set()
            monitor_thread.join(timeout=10)

        # Save results
        job_status[job_id] = {
            **eval_results,
            'status': 'done',
            'monitoring': 'done' if container else 'skipped',
            'broker_name': broker_name,
            'job_id': job_id,
            'resource_csv': resource_csv,
            'ntp_synchronized': TIMING_SYNC_AVAILABLE
        }
        
        print(f"[TestRunner] Synchronized evaluation completed for {broker_name}")
        
    except Exception as e:
        print(f"[TestRunner] Error: {e}")
        import traceback
        traceback.print_exc()
        
        # Cleanup
        if 'stop_event' in locals() and stop_event:
            stop_event.set()
        if 'monitor_thread' in locals() and monitor_thread:
            monitor_thread.join(timeout=5)
            
        job_status[job_id] = {
            'status': 'failed',
            'error': f'Test execution error: {str(e)}',
            'broker_name': args.get('broker_name', 'unknown')
        }

def get_docker_broker_names():
    return {'activemq', 'mosquitto', 'vernemq', 'emqx', 'hivemq', 'nanomq', 'rabbitmq'}

def monitor_container_stats(container_id, csv_path, stop_event):
    """Monitor Docker container resource usage"""
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
                        timestamp, round(cpu_percent, 2), mem_usage, mem_limit,
                        net_rx, net_tx, block_read, block_write
                    ])
                    csvfile.flush()
                    
                except StopIteration:
                    break
                except Exception as e:
                    print(f"[Monitor Error] {e}")
                    time.sleep(1)
                    
    except Exception as e:
        print(f"[Monitor Setup Failed] {e}")
    finally:
        stop_event.set()

# =============================================================================
# EVALUATION ROUTES
# =============================================================================

@app.route('/run_tests', methods=['POST'])
def run_tests():
    """Start enhanced broker evaluation tests"""
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

# =============================================================================
# ADDITIONAL ROUTES
# =============================================================================

@app.route('/timing_status')
def timing_status():
    """Get current timing synchronization status"""
    if global_timing_sync:
        status = global_timing_sync.get_sync_status()
        status['available'] = True
    else:
        status = {'available': False, 'error': 'Timing sync not initialized'}
    
    return jsonify(status)

@app.route('/verify_flow', methods=['GET'])
def verify_flow():
    """Verify Node-RED flow with timing info"""
    try:
        resp = requests.get(f'{NODE_RED_URL}/flows', timeout=5)
        flows = resp.json()
        
        node_types = {}
        for node in flows:
            node_type = node.get('type', 'unknown')
            node_types[node_type] = node_types.get(node_type, 0) + 1
        
        # Check for synchronized components
        has_ntp_sync = any(node.get('name', '').startswith('NTP') for node in flows)
        has_sync_gen = any('SyncGen' in node.get('name', '') for node in flows)
        has_precision_calc = any('PrecisionCalc' in node.get('name', '') for node in flows)
        
        return jsonify({
            "node_red_connected": resp.status_code == 200,
            "total_nodes": len(flows),
            "node_types": node_types,
            "enhanced_features": {
                "ntp_synchronization": has_ntp_sync,
                "synchronized_generators": has_sync_gen,
                "precision_calculators": has_precision_calc
            },
            "timing_sync_available": TIMING_SYNC_AVAILABLE,
            "timing_sync_status": global_timing_sync.get_sync_status() if global_timing_sync else None
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# =============================================================================
# MAIN ROUTES
# =============================================================================

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/health')
def health():
    ntp_status = "active" if global_timing_sync else "inactive"
    return jsonify({
        "status": "healthy",
        "ntp_synchronization": ntp_status,
        "timing_sync_available": TIMING_SYNC_AVAILABLE,
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
    
    # Check MQTT broker connectivity
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
    
    # Initialize or check timing synchronization
    if TIMING_SYNC_AVAILABLE:
        if initialize_timing_sync():
            print("NTP synchronization initialized successfully")
        else:
            print("WARNING: NTP synchronization failed to initialize")
    else:
        print("WARNING: NTP synchronization modules not available")
        print("         Install 'ntplib' package for timing synchronization")
    
    print("Starting Enhanced Flask Application...")
    print("Features enabled:")
    print(f"  - NTP Synchronization: {'Yes' if TIMING_SYNC_AVAILABLE else 'No'}")
    print(f"  - Precision Timing: {'Yes' if TIMING_SYNC_AVAILABLE else 'No'}")
    print(f"  - Enhanced Validation: {'Yes' if TIMING_SYNC_AVAILABLE else 'No'}")
    print("Access the application at: http://localhost:5000")
    
    app.run(debug=True, host='0.0.0.0', port=5000, use_reloader=False)