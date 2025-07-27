class TopicManager:
    def __init__(self):
        self.topics = set()
        self.groups = {}

    def add_topic(self, topic_name: str):
        """Add a single topic"""
        self.topics.add(topic_name)

    def create_group(self, group_name: str, count: int):
        """Create a group of N topics, like Temp_1, Temp_2, ..."""
        group_topics = [f"{group_name}_Topic{i+1}" for i in range(count)]
        self.groups[group_name] = group_topics
        self.topics.update(group_topics)

    def get_all_topics(self):
        """Return sorted list of all topics"""
        return sorted(self.topics)

    def get_group(self, group_name: str):
        """Return the topics in a specific group"""
        return self.groups.get(group_name, [])
    
    def remove_topic(self, topic_name: str):
        """Remove a single topic (from both topics and any groups)"""
        removed = topic_name in self.topics
        self.topics.discard(topic_name)

        for group in list(self.groups):
            if topic_name in self.groups[group]:
                self.groups[group].remove(topic_name)
                if not self.groups[group]:
                    del self.groups[group]
        return removed

    def reset(self):
        """Clear all topics and groups"""
        self.topics.clear()
        self.groups.clear()
