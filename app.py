from deployment.topic_manager import TopicManager
from deployment.group_expander import GroupExpander
import paho.mqtt.client as mqtt

from threading import Thread
import time
import paho.mqtt.client as mqtt
from collections import deque

import csv
import os
import sys
import glob
import json
import uuid
import threading
import subprocess
import requests
import docker
import time
from datetime import datetime

from flask import (
    Flask, render_template, request,
    send_from_directory, jsonify
)

app = Flask(__name__, template_folder="frontend/templates")

def new_id():
    return uuid.uuid4().hex[:8]


job_status = {}

# def check_broker_reachability(host, port, timeout=3):
#     client = mqtt.Client()
#     try:
#         client.connect(host, port, keepalive=timeout)
#         client.disconnect()
#         return True
#     except Exception as e:
#         print(f"[BROKER CHECK] Failed to reach {host}:{port} - {e}")
#         return False


# Node-RED Admin API URL
NODE_RED_URL = 'http://localhost:1880'

# Broker container IDs
BROKER_IDS = {
    'mosquitto': 'f9d12cd8dcabc8fcad6f5ab68c9a9b8e9a5ed018e18385d55c3dd941109a3690',
    'activemq': 'cf0a288b8762ffc521693e234a2507e93ecd7ccaea17e5e5a0faa89ff80227a4',
    'nanomq': '7c1c0838010b887742305d6a8a73ba3c5ad435d951f1a8ceb07e1edc5e9c1f1b',
    'hivemq': 'c2cd7bbef9eb24857933a08830afdc7544fd3001839d0ac1f7af5a36b7c9b8b6',
    'emqx': 'd51a342d9f098ce4452d64b11373c896a7ada477ec7d2ef446f51db1624f01e4',
    'rabbitmq': 'b9eae0064bf3aaabd8438e6a4269db4bdf9c7970eec384b8f824111c6e7fd22a',
    'vernemq': '637ccaec617e7b403f984ec4f8c6961aebb995f024db451a1de94eb94c3723ea'
}

topic_manager = TopicManager()

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

@app.route('/expand_groups', methods=['POST'])
def expand_groups():
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


@app.route('/deploy_simulation', methods=['POST'])
def deploy_simulation():
    from deployment.group_expander import GroupExpander
    import requests

    data = request.get_json()

    publisher_groups = data.get("publisher_groups", [])
    subscriber_groups = data.get("subscriber_groups", []) 

    # --- Check broker rechability commented now because of the conflict between the Docker network and Flask --- 

    broker_host = data.get("broker_name", "localhost")
    broker_port = int(data.get("broker_port", 1883))

    # if not check_broker_reachability(broker_host, broker_port):
    #     return jsonify(error=f"Cannot connect to MQTT broker at {broker_host}:{broker_port}"), 400


    # Expand groups
    pub_expander = GroupExpander(mode="publisher")
    pub_instances, pub_warnings = pub_expander.expand(publisher_groups)

    sub_expander = GroupExpander(mode="subscriber")
    sub_instances, sub_warnings = sub_expander.expand(subscriber_groups)

    all_nodes = []
    wires = {}

    tab_id = new_id()
    all_nodes.append({
        "id": tab_id,
        "type": "tab",
        "label": "Sim-AutoFlow",
        "disabled": False,
        "info": ""
    })

    # MQTT broker config node
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

    # Add publishers
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
                "retain": pub.get("retain", False),
                "broker": broker_config_id,
                "x": 560,
                "y": y,
                "wires": []
            }
        ])
        y += 60

    # Add subscribers
    for sub in sub_instances:
        x = 140
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
            'http://localhost:1880/flows',
            headers={'Content-Type': 'application/json'},
            json=all_nodes
        )
        if resp.status_code == 204:
            return jsonify(ok=True, warnings=pub_warnings + sub_warnings)
        else:
            return jsonify(error=f"Failed to deploy: {resp.text}"), 500
    except Exception as e:
        return jsonify(error=str(e)), 500
    

@app.route('/simulation/<action>', methods=['POST'])
def control_simulation(action):
    import requests

    if action not in ('start', 'stop'):
        return jsonify({"error": "Invalid action"}), 400

    try:
        # 1. Get all current flows
        flows = requests.get('http://localhost:1880/flows').json()

        # 2. Toggle inject nodes
        for node in flows:
            if node.get("type") == "inject":
                node["disabled"] = (action == "stop")

        # 3. Redeploy with updated inject states
        resp = requests.post(
            'http://localhost:1880/flows',
            headers={'Content-Type': 'application/json'},
            json=flows
        )

        if resp.status_code == 204:
            return jsonify(ok=True, action=action)
        else:
            return jsonify(error=resp.text), 500

    except Exception as e:
        return jsonify(error=str(e)), 500
    

delay_data = deque(maxlen=2000)

def on_message(client, userdata, msg):
    try:
        import json
        payload = json.loads(msg.payload.decode())
        payload['timestamp'] = time.time()
        delay_data.append(payload)
    except Exception as e:
        print(f"[Delay Parser Error] {e}")

def start_delay_collector(broker='localhost', port=1883):
    client = mqtt.Client()
    client.on_message = on_message

    try:
        client.connect(broker, port, 60)
    except Exception as e:
        print(f"❌ Failed to connect to broker: {e}")
        return

    client.subscribe("sim/stats/delay")
    client.loop_start()
    print("✅ Delay collector started")

# Optional: call it on startup (or trigger via /start endpoint)
# Thread(target=start_delay_collector, args=('localhost', 1883), daemon=True).start()

@app.route('/api/metrics')
def get_delay_metrics():
    # Return latest 100 records
    return jsonify(list(delay_data)[-100:])


@app.route('/results/<broker_name>')
def results(broker_name):
    # Slice the delay metrics to the most recent 100 records
    delay_samples = list(delay_data)[-100:]

    return render_template("results.html",
        broker_name=broker_name,
        delay_samples=delay_samples
    )




@app.route('/')
def index():
    return render_template('index.html')

if __name__ == '__main__':
    app.run(debug=True)
