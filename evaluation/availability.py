import logging
import socket
import time
from statistics import mean

logger = logging.getLogger(__name__)


class AvailabilityMonitor:
    def __init__(self, check_interval=2, failure_threshold=2):
        self.check_interval = check_interval
        self.failure_threshold = failure_threshold
        self.failures = []
        self.recoveries = []
        self.downtime_events = []
        self.total_checks = 0
        self.failed_checks = 0

    def check_broker(self, broker_host, broker_port):
        try:
            sock = socket.create_connection((broker_host, broker_port), timeout=1)
            sock.close()
            return True
        except Exception:
            return False

    def monitor(self, broker_host, broker_port, duration):
        logger.info("Starting availability monitoring of %s:%d for %ds", 
                   broker_host, broker_port, duration)
        start_time = time.time()
        consecutive_failures = 0
        failure_start = None
        first_failure_time = None

        while time.time() - start_time < duration:
            reachable = self.check_broker(broker_host, broker_port)
            self.total_checks += 1

            if reachable:
                if consecutive_failures >= self.failure_threshold and failure_start:
                    recovery_time = time.time()
                    self.recoveries.append(recovery_time)
                    downtime = recovery_time - failure_start
                    self.downtime_events.append(downtime)
                    logger.info("Broker recovered after %.2fs downtime", downtime)
                    failure_start = None
                    first_failure_time = None
                consecutive_failures = 0
            else:
                self.failed_checks += 1
                if consecutive_failures == 0:
                    first_failure_time = time.time()
                consecutive_failures += 1
                if consecutive_failures == self.failure_threshold:
                    failure_start = first_failure_time
                    self.failures.append(failure_start)
                    logger.warning("Broker failure detected at check #%d", self.total_checks)

            time.sleep(self.check_interval)
        
        logger.info("Monitoring complete. Total checks: %d, Failed: %d", 
                   self.total_checks, self.failed_checks)

    def get_stats(self):
        return {
            "total_checks": self.total_checks,
            "failures": len(self.failures),
            "recoveries": len(self.recoveries),
            "downtime_events": len(self.downtime_events),
            "avg_downtime_sec": round(mean(self.downtime_events), 2) if self.downtime_events else 0.0,
            "mtbf_sec": round(mean(self._durations_between(self.failures)), 2) if len(self.failures) > 1 else 0.0,
            "mttr_sec": round(mean(self.downtime_events), 2) if self.downtime_events else 0.0
        }

    def _durations_between(self, timestamps):
        return [t2 - t1 for t1, t2 in zip(timestamps[:-1], timestamps[1:])]