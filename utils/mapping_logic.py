def enforce_1to1_mapping(instance_count: int, topic_count: int):
    if instance_count == topic_count:
        return instance_count, None
    elif instance_count > topic_count:
        return topic_count, f"Only {topic_count} topics selected, so only the first {topic_count} of {instance_count} instances will be active."
    else:
        return instance_count, f"{topic_count} topics selected, but only {instance_count} instances provided. Only the first {instance_count} topics will be used."
