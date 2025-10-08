import time
from collections import deque
from statistics import mean, stdev


class LatencyTracker:
    """
    EMQX-style Latency Tracker:
    ---------------------------------
    - No longer depends on MQTT messages.
    - Receives precomputed delay measurements via Flask's /api/latency endpoint.
    - Aggregates delays efficiently in memory.
    - Computes mean, jitter, and percentile stats.
    """

    def __init__(self):
        self.delays = deque()  # Stores delay values (ms)
        self.timestamps = deque()  # Optional, for tracking when data arrived
        self.processed_count = 0
        self.error_count = 0

        # Per-publisher aggregation (optional if publisher info is included)
        self.publisher_message_count = {}
        self.last_update = time.time()

    def handle_message(self, record: dict):
        """
        Handle a single latency record from Flask's HTTP collector.
        Expected format:
            {
                "subscriber": "Sub_1",
                "topic": "sensors/temp",
                "delay": 15.3,
                "timestamp": 1738961290.12
            }
        """
        try:
            delay = record.get("delay")
            if delay is None:
                self.error_count += 1
                return

            self.processed_count += 1
            delay = float(delay)
            if delay < 0 or delay > 60000:  # Sanity check
                self.error_count += 1
                return

            self.delays.append(delay)
            self.timestamps.append(record.get("timestamp", time.time()))

            # Optional: count per subscriber
            subscriber = record.get("subscriber", "unknown")
            self.publisher_message_count[subscriber] = (
                self.publisher_message_count.get(subscriber, 0) + 1
            )

            # Periodic logging for visibility
            if len(self.delays) % 500 == 0:
                avg_delay = mean(list(self.delays)[-500:])
                print(
                    f"[LatencyTracker] Processed {len(self.delays)} samples | "
                    f"Recent avg={avg_delay:.2f} ms | Errors={self.error_count}"
                )

        except Exception as e:
            self.error_count += 1
            print(f"[LatencyTracker] Error processing record: {e}")

    def _calculate_percentiles(self, delays_list):
        """Calculate P50, P95, P99 percentiles"""
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

    def get_stats(self):
        """Compute and return aggregated latency statistics"""
        total = len(self.delays)
        print(f"[LatencyTracker] Generating latency stats for {total} samples...")

        if total == 0:
            return {
                "count": 0,
                "processed_count": self.processed_count,
                "error_count": self.error_count,
                "error_rate": 0.0,
                "avg_delay": 0.0,
                "min_delay": 0.0,
                "max_delay": 0.0,
                "jitter": 0.0,
                "p50": 0.0,
                "p95": 0.0,
                "p99": 0.0,
                "publisher_breakdown": {},
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
                "avg_delay": round(avg_delay, 2),
                "min_delay": round(min(delays_list), 2),
                "max_delay": round(max(delays_list), 2),
                "jitter": round(jitter, 2),
                "p50": p50,
                "p95": p95,
                "p99": p99,
                "publisher_breakdown": dict(self.publisher_message_count),
            }

            print(
                f"[LatencyTracker] Stats -> avg={stats['avg_delay']}ms | "
                f"p50={p50} | p95={p95} | p99={p99} | jitter={stats['jitter']}"
            )
            return stats

        except Exception as e:
            print(f"[LatencyTracker] Error computing stats: {e}")
            return {
                "count": total,
                "processed_count": self.processed_count,
                "error_count": self.error_count + 1,
                "error_rate": 100.0,
                "avg_delay": 0.0,
                "min_delay": 0.0,
                "max_delay": 0.0,
                "jitter": 0.0,
                "p50": 0.0,
                "p95": 0.0,
                "p99": 0.0,
                "publisher_breakdown": dict(self.publisher_message_count),
            }
