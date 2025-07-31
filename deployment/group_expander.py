from utils.mapping_logic import enforce_1to1_mapping

class GroupExpander:
    def __init__(self, mode="publisher"):
        self.mode = mode  # or "subscriber"

    def expand(self, groups: list[dict]):
        expanded = []
        warnings = []

        for group in groups:
            base_name = group["group_name"]
            count = int(group.get("count", 1))
            topics = group.get("topics", [])

            # Publishers: enforce 1:1
            if self.mode == "publisher":
                actual_count, warning = enforce_1to1_mapping(count, len(topics))
                if warning:
                    warnings.append(f"[{base_name}] {warning}")
                for i in range(actual_count):
                    name = base_name if count == 1 else f"{base_name}_{i+1}"
                    instance = {
                        "name": name,
                        "group": base_name,
                        "topic": topics[i],
                        "interval": group.get("frequency", 1.0),
                        "payload_size": group.get("payload_size", 256),
                        "qos": group.get("qos", 1),
                        "retain": group.get("retain", False)
                    }
                    expanded.append(instance)

            # Subscribers: assign all selected topics to each subscriber
            elif self.mode == "subscriber":
                for i in range(count):
                    name = base_name if count == 1 else f"{base_name}_{i+1}"
                    instance = {
                        "name": name,
                        "topics": topics  # multiple topics per subscriber
                    }
                    expanded.append(instance)

        return expanded, warnings
