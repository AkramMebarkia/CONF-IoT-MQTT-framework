import logging
import time
from collections import deque, defaultdict
from statistics import mean, stdev

logger = logging.getLogger(__name__)


class EnhancedLatencyTracker:
    """
    Enhanced latency tracker with stratified sampling and better statistics
    """

    def __init__(self):
        self.delays = deque()
        self.timestamps = deque()
        self.processed_count = 0
        self.error_count = 0
        self.duplicate_count = 0

        self.publisher_delays = defaultdict(deque)
        self.topic_delays = defaultdict(deque)
        self.subscriber_delays = defaultdict(deque)
        
        self.unique_messages = set()
        self.publisher_message_count = defaultdict(int)
        
        # QoS 2 deduplication: track (publisher, seq_id, subscriber) to detect retries
        self.seen_deliveries = set()
        
        self.last_update = time.time()

    def handle_message(self, record: dict):
        """
        Handle a single latency record with complete metadata
        """
        try:
            delay = record.get("delay")
            if delay is None:
                self.error_count += 1
                return

            # QoS 2 deduplication check
            publisher = record.get("publisher_name", "unknown")
            seq_id = record.get("seq_id")
            subscriber = record.get("subscriber", "unknown")
            
            if seq_id is not None:
                delivery_key = (publisher, seq_id, subscriber)
                if delivery_key in self.seen_deliveries:
                    self.duplicate_count += 1
                    return
                self.seen_deliveries.add(delivery_key)

            self.processed_count += 1
            delay = float(delay)
            
            if delay < 0 or delay > 60000:
                self.error_count += 1
                return

            # Store in main collection
            self.delays.append(delay)
            self.timestamps.append(record.get("timestamp", time.time()))

            # Extract metadata
            publisher = record.get("publisher_name", "unknown")
            seq_id = record.get("seq_id")
            topic = record.get("topic", "unknown")
            subscriber = record.get("subscriber", "unknown")

            # Track unique publisher messages
            if publisher != "unknown" and seq_id is not None:
                message_key = (publisher, seq_id)
                if message_key not in self.unique_messages:
                    self.unique_messages.add(message_key)
                    self.publisher_message_count[publisher] += 1

            # Store in stratified collections (limit per category to prevent memory issues)
            MAX_PER_CATEGORY = 1000
            
            if len(self.publisher_delays[publisher]) < MAX_PER_CATEGORY:
                self.publisher_delays[publisher].append(delay)
            
            if len(self.topic_delays[topic]) < MAX_PER_CATEGORY:
                self.topic_delays[topic].append(delay)
            
            if len(self.subscriber_delays[subscriber]) < MAX_PER_CATEGORY:
                self.subscriber_delays[subscriber].append(delay)

            if len(self.delays) % 500 == 0:
                recent_avg = mean(list(self.delays)[-500:])
                unique_pubs = len(set(k[0] for k in self.unique_messages))
                logger.debug("Samples: %d | Unique msgs: %d | Publishers: %d | Recent avg: %.2fms",
                            len(self.delays), len(self.unique_messages), unique_pubs, recent_avg)

        except Exception as e:
            self.error_count += 1
            logger.error("Error processing latency record: %s", e)

    def reset(self):
        """Reset all collected data for fresh start after warm-up"""
        self.delays.clear()
        self.timestamps.clear()
        self.processed_count = 0
        self.error_count = 0
        self.duplicate_count = 0
        self.publisher_delays.clear()
        self.topic_delays.clear()
        self.subscriber_delays.clear()
        self.unique_messages.clear()
        self.publisher_message_count.clear()
        self.seen_deliveries.clear()
        self.last_update = time.time()
        logger.info("Latency tracker reset")

    def _calculate_percentiles(self, delays_list):
        """Calculate P50, P95, P99 percentiles with proper interpolation"""
        if not delays_list or len(delays_list) < 2:
            return 0.0, 0.0, 0.0

        sorted_delays = sorted(delays_list)
        n = len(sorted_delays)

        def get_percentile(p):
            k = (n - 1) * p
            f = int(k)
            c = min(f + 1, n - 1)
            return sorted_delays[f] + (sorted_delays[c] - sorted_delays[f]) * (k - f)

        return (
            round(get_percentile(0.50), 2),
            round(get_percentile(0.95), 2),
            round(get_percentile(0.99), 2),
        )

    def get_stratified_stats(self):
        """Get statistics broken down by publisher, topic, and subscriber"""
        stratified = {
            "by_publisher": {},
            "by_topic": {},
            "by_subscriber": {}
        }

        # Publisher statistics
        for pub, delays in self.publisher_delays.items():
            if delays:
                delays_list = list(delays)
                p50, p95, p99 = self._calculate_percentiles(delays_list)
                stratified["by_publisher"][pub] = {
                    "count": len(delays_list),
                    "avg": round(mean(delays_list), 2),
                    "min": round(min(delays_list), 2),
                    "max": round(max(delays_list), 2),
                    "p50": p50,
                    "p95": p95,
                    "p99": p99
                }

        # Topic statistics
        for topic, delays in self.topic_delays.items():
            if delays:
                delays_list = list(delays)
                p50, p95, p99 = self._calculate_percentiles(delays_list)
                stratified["by_topic"][topic] = {
                    "count": len(delays_list),
                    "avg": round(mean(delays_list), 2),
                    "p50": p50,
                    "p95": p95
                }

        # Subscriber statistics
        for sub, delays in self.subscriber_delays.items():
            if delays:
                delays_list = list(delays)
                stratified["by_subscriber"][sub] = {
                    "count": len(delays_list),
                    "avg": round(mean(delays_list), 2)
                }

        return stratified

    def get_stats(self):
        """Compute and return comprehensive latency statistics"""
        total = len(self.delays)
        print(f"[LatencyTracker] Generating stats for {total} samples, {len(self.unique_messages)} unique messages...")

        if total == 0:
            return {
                "count": 0,
                "processed_count": self.processed_count,
                "error_count": self.error_count,
                "error_rate": 0.0,
                "unique_publisher_messages": 0,
                "avg_delay": 0.0,
                "min_delay": 0.0,
                "max_delay": 0.0,
                "jitter": 0.0,
                "p50": 0.0,
                "p95": 0.0,
                "p99": 0.0,
                "publisher_breakdown": {},
                "stratified_stats": {}
            }

        delays_list = list(self.delays)
        try:
            p50, p95, p99 = self._calculate_percentiles(delays_list)
            avg_delay = mean(delays_list)
            jitter = stdev(delays_list) if len(delays_list) > 1 else 0.0
            error_rate = (self.error_count / max(self.processed_count, 1)) * 100

            stats = {
                "count": total,
                "processed_count": self.processed_count,
                "error_count": self.error_count,
                "error_rate": round(error_rate, 2),
                "unique_publisher_messages": len(self.unique_messages),
                "avg_delay": round(avg_delay, 2),
                "min_delay": round(min(delays_list), 2),
                "max_delay": round(max(delays_list), 2),
                "jitter": round(jitter, 2),
                "p50": p50,
                "p95": p95,
                "p99": p99,
                "publisher_breakdown": dict(self.publisher_message_count),
                "stratified_stats": self.get_stratified_stats()
            }

            print(
                f"[LatencyTracker] Final stats: {len(self.unique_messages)} unique messages | "
                f"avg={stats['avg_delay']}ms | p50={p50} | p95={p95} | p99={p99}"
            )
            return stats

        except Exception as e:
            print(f"[LatencyTracker] Error computing stats: {e}")
            import traceback
            traceback.print_exc()
            return {
                "count": total,
                "processed_count": self.processed_count,
                "error_count": self.error_count + 1,
                "error_rate": 100.0,
                "unique_publisher_messages": len(self.unique_messages),
                "avg_delay": 0.0,
                "min_delay": 0.0,
                "max_delay": 0.0,
                "jitter": 0.0,
                "p50": 0.0,
                "p95": 0.0,
                "p99": 0.0,
                "publisher_breakdown": dict(self.publisher_message_count),
                "stratified_stats": {}
            }