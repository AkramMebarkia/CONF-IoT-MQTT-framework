import json
import os
from datetime import datetime

class StatsAggregator:
    def __init__(self, job_id, broker_name, output_dir="results"):
        self.job_id = job_id
        self.broker_name = broker_name
        self.metrics = {}
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def add_module_stats(self, module_name, stats_dict):
        """Merge results from a module (e.g., latency, throughput)"""
        self.metrics[module_name] = stats_dict

    def flatten(self):
        """Return flattened metrics for easy viewing/rendering"""
        flat = {
            "job_id": self.job_id,
            "broker_name": self.broker_name,
            "timestamp": datetime.utcnow().isoformat()
        }
        for module, stats in self.metrics.items():
            for k, v in stats.items():
                flat[f"{module}_{k}"] = v
        return flat

    def save_json(self):
        """Write to results/job_<id>.json"""
        flat = self.flatten()
        path = os.path.join(self.output_dir, f"job_{self.job_id}.json")
        with open(path, "w") as f:
            json.dump(flat, f, indent=2)
        return path

    def get_summary(self):
        """Return flattened metrics + path"""
        path = self.save_json()
        return {
            "summary": self.flatten(),
            "saved_to": path
        }