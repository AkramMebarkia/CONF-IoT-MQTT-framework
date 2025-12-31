def topic_matches_pattern(topic: str, pattern: str) -> bool:
    """
    Check if a concrete topic matches an MQTT subscription pattern with wildcards.
    
    Args:
        topic: Concrete topic like "sensor/room1/temperature"
        pattern: Subscription pattern like "sensor/+/temperature" or "sensor/#"
    
    Returns:
        True if topic matches the pattern
    
    MQTT Wildcards:
        + : matches exactly one level
        # : matches zero or more levels (must be last)
    
    Examples:
        topic_matches_pattern("sensor/room1/temp", "sensor/+/temp") -> True
        topic_matches_pattern("sensor/room1/temp", "sensor/#") -> True
        topic_matches_pattern("sensor/room1/temp", "device/+/temp") -> False
    """
    if pattern == "#":
        return True
    
    if pattern == topic:
        return True
    
    pattern_parts = pattern.split('/')
    topic_parts = topic.split('/')
    
    for i, p in enumerate(pattern_parts):
        if p == '#':
            return True
        if p == '+':
            if i >= len(topic_parts):
                return False
            continue
        if i >= len(topic_parts) or topic_parts[i] != p:
            return False
    
    return len(pattern_parts) == len(topic_parts)


def enforce_smart_mapping(instance_count: int, topic_count: int, mode: str = "round_robin"):
    """
    Smart mapping strategies for publisher-to-topic assignment
    
    Modes:
    - round_robin: Distribute publishers across topics evenly
    - broadcast: All publishers can publish to all topics
    - partition: Divide publishers into topic groups
    - weighted: Assign more publishers to certain topics
    """
    
    if mode == "1to1":
        # Legacy mode - kept for backward compatibility
        if instance_count == topic_count:
            return instance_count, None, "1to1"
        elif instance_count > topic_count:
            return topic_count, f"Only {topic_count} topics selected, reducing publishers to {topic_count}", "1to1"
        else:
            return instance_count, f"Only using first {instance_count} topics", "1to1"
    
    elif mode == "round_robin":
        # All publishers are active, distributed across topics
        return instance_count, None, "round_robin"
    
    elif mode == "broadcast":
        # Each publisher publishes to all topics (creates instance_count * topic_count messages)
        return instance_count, None, "broadcast"
    
    elif mode == "partition":
        # Divide publishers into groups, each group gets subset of topics
        if instance_count < topic_count:
            return instance_count, f"Partition mode: each publisher gets multiple topics", "partition"
        else:
            return instance_count, None, "partition"
    
    else:
        # Default to round_robin
        return instance_count, None, "round_robin"


def assign_topics_to_publishers(publishers: list, topics: list, mode: str = "round_robin"):
    """
    Assign topics to publishers based on the selected strategy
    
    Returns: list of (publisher_index, topic) tuples
    """
    assignments = []
    
    if not publishers or not topics:
        return assignments
    
    if mode == "1to1":
        # Original 1:1 mapping
        for i in range(min(len(publishers), len(topics))):
            assignments.append((i, topics[i]))
    
    elif mode == "round_robin":
        # Distribute publishers across topics evenly
        for i, pub in enumerate(publishers):
            topic_idx = i % len(topics)
            assignments.append((i, topics[topic_idx]))
    
    elif mode == "broadcast":
        # Each publisher publishes to all topics
        for i, pub in enumerate(publishers):
            for topic in topics:
                assignments.append((i, topic))
    
    elif mode == "partition":
        # Partition publishers into groups
        pubs_per_topic = max(1, len(publishers) // len(topics))
        remainder = len(publishers) % len(topics)
        
        pub_idx = 0
        for topic_idx, topic in enumerate(topics):
            # Add extra publisher to first 'remainder' topics
            count = pubs_per_topic + (1 if topic_idx < remainder else 0)
            for _ in range(count):
                if pub_idx < len(publishers):
                    assignments.append((pub_idx, topic))
                    pub_idx += 1
    
    return assignments


def calculate_expected_messages(config: dict, duration: int = 60) -> dict:
    """Calculate theoretical expected message counts"""
    expected = {"publishers": {}, "total_publisher_messages": 0}
    
    for group in config.get("publisher_groups", []):
        group_name = group["group_name"]
        count = group["count"] 
        interval = group["frequency"]  # This is interval in seconds
        topics = group["topics"]
        
        # Messages per publisher = duration / interval
        msgs_per_pub = duration / interval
        total_group_msgs = count * msgs_per_pub
        
        expected["publishers"][group_name] = {
            "publishers": count,
            "interval": interval,
            "expected_per_publisher": round(msgs_per_pub),
            "total_expected": round(total_group_msgs)
        }
        
        expected["total_publisher_messages"] += total_group_msgs
        
        print(f"Expected: {group_name} -> {count} pubs × {msgs_per_pub:.0f} msgs = {total_group_msgs:.0f} total")
    
    return expected