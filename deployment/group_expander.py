from utils.mapping_logic import enforce_1to1_mapping

class GroupExpander:
    def __init__(self, mode="publisher"):
        self.mode = mode  # "publisher" or "subscriber"

    def expand(self, groups: list[dict]):
        expanded = []
        warnings = []

        print(f"🔧 [GroupExpander] Expanding {len(groups)} groups for {self.mode}s")

        for group in groups:
            base_name = group.get("group_name", "Unnamed")
            count = int(group.get("count", 1))
            topics = group.get("topics", [])

            print(f"   🔧 Processing group '{base_name}': {count} instances, {len(topics)} topics")

            # Publishers: enforce 1:1 mapping
            if self.mode == "publisher":
                actual_count, warning = enforce_1to1_mapping(count, len(topics))
                if warning:
                    warning_msg = f"[{base_name}] {warning}"
                    warnings.append(warning_msg)
                    print(f"   ⚠️  {warning_msg}")

                for i in range(actual_count):
                    name = base_name if count == 1 else f"{base_name}_{i+1}"
                    instance = {
                        "name": name,
                        "group": base_name,
                        "topic": topics[i],  # Each publisher gets one topic
                        "interval": group.get("frequency", 1.0),
                        "payload_size": group.get("payload_size", 256),
                        "qos": group.get("qos", 1),
                        "retain": group.get("retain", False)
                    }
                    expanded.append(instance)
                    print(f"     ✅ Publisher: {name} → {topics[i]}")

            # Subscribers: assign all selected topics to each subscriber
            elif self.mode == "subscriber":
                for i in range(count):
                    name = base_name if count == 1 else f"{base_name}_{i+1}"
                    instance = {
                        "name": name,
                        "group": base_name,
                        "topics": topics,  # Each subscriber gets all topics
                        "qos": group.get("qos", 1)
                    }
                    expanded.append(instance)
                    print(f"     ✅ Subscriber: {name} ← {len(topics)} topics")

        print(f"🔧 [GroupExpander] Expansion complete: {len(expanded)} instances created")
        return expanded, warnings