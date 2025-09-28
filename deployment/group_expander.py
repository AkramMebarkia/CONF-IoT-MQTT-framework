from utils.mapping_logic import assign_topics_to_publishers, enforce_smart_mapping

class GroupExpander:
    def __init__(self, mode="publisher", mapping_strategy="round_robin"):
        self.mode = mode  # "publisher" or "subscriber"
        self.mapping_strategy = mapping_strategy  # Mapping strategy for publishers

    def expand(self, groups: list[dict]):
        expanded = []
        warnings = []
        stats = {}

        print(f"[GroupExpander] Expanding {len(groups)} groups for {self.mode}s")
        print(f"Mapping strategy: {self.mapping_strategy}")

        for group in groups:
            base_name = group.get("group_name", "Unnamed")
            count = int(group.get("count", 1))
            topics = group.get("topics", [])
            
            # Get mapping strategy - allow per-group override
            group_strategy = group.get("mapping", self.mapping_strategy)

            print(f"   Processing group '{base_name}': {count} instances, {len(topics)} topics, strategy: {group_strategy}")

            # Publishers: use smart mapping
            if self.mode == "publisher":
                # Create all publisher instances first
                publishers = []
                for i in range(count):
                    name = base_name if count == 1 else f"{base_name}_{i+1}"
                    publishers.append(name)
                
                # Get topic assignments using smart mapping
                assignments = assign_topics_to_publishers(publishers, topics, group_strategy)
                
                # Track statistics
                stats[base_name] = {
                    "total_publishers": count,
                    "active_publishers": len(set(a[0] for a in assignments)),
                    "total_topics": len(topics),
                    "assignments": len(assignments),
                    "strategy": group_strategy
                }
                
                # Create publisher instances based on assignments
                for pub_idx, topic in assignments:
                    pub_name = publishers[pub_idx]
                    instance = {
                        "name": pub_name,
                        "group": base_name,
                        "topic": topic,
                        "interval": group.get("frequency", 1.0),
                        "payload_size": group.get("payload_size", 256),
                        "qos": group.get("qos", 1),
                        "retain": group.get("retain", False),
                        "original_index": pub_idx  # Track original publisher index
                    }
                    expanded.append(instance)
                    
                print(f"     Created {len(assignments)} publisher assignments")
                print(f"        Active publishers: {stats[base_name]['active_publishers']}/{count}")
                
                # Add warning if not all publishers are active (only for 1to1 mode)
                if group_strategy == "1to1" and stats[base_name]['active_publishers'] < count:
                    warning = f"[{base_name}] Only {stats[base_name]['active_publishers']}/{count} publishers active due to 1:1 mapping"
                    warnings.append(warning)
                    print(f"{warning}")

            # Subscribers: assign all selected topics to each subscriber (unchanged)
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
                    print(f"     Subscriber: {name} ← {len(topics)} topics")

        print(f"[GroupExpander] Expansion complete: {len(expanded)} instances created")
        if self.mode == "publisher":
            print(f"   Publisher Statistics:")
            for group_name, group_stats in stats.items():
                print(f"      {group_name}: {group_stats['assignments']} assignments from {group_stats['total_publishers']} publishers")
        
        return expanded, warnings