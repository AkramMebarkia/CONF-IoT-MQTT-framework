import logging
import socket
import threading
import time

import docker

logger = logging.getLogger(__name__)


class CrashEvent:
    """Represents a single completed crash-recovery cycle."""

    def __init__(self, scheduled_time, scheduled_duration):
        self.scheduled_time = scheduled_time        # When crash was supposed to happen (relative seconds)
        self.scheduled_duration = scheduled_duration # How long broker should stay down

        # Filled in at runtime
        self.actual_crash_time = None      # Absolute timestamp when container was stopped
        self.actual_recovery_time = None   # Absolute timestamp when container responded again
        self.container_start_time = None   # Absolute timestamp when docker start was issued
        self.actual_downtime = 0.0         # Seconds the broker was actually unreachable
        self.recovery_latency = 0.0        # Extra seconds beyond scheduled duration for port to respond
        self.success = False
        self.error = None

    def to_dict(self):
        return {
            "scheduled_time": self.scheduled_time,
            "scheduled_duration": self.scheduled_duration,
            "actual_crash_time": self.actual_crash_time,
            "actual_recovery_time": self.actual_recovery_time,
            "container_start_time": self.container_start_time,
            "actual_downtime": round(self.actual_downtime, 2),
            "recovery_latency": round(self.recovery_latency, 2),
            "success": self.success,
            "error": self.error,
        }


class CrashInjector:
    """
    Orchestrates timed broker crash-recovery cycles during an evaluation.

    The injector runs in a background thread.  For each scheduled crash it:
      1. Waits until the target time (relative to measurement start).
      2. Stops the Docker container (``docker stop``).
      3. Waits the configured crash duration.
      4. Restarts the container (``docker start``).
      5. Polls the broker port until it accepts connections.
      6. Records precise timestamps for correlation with latency data.
    """

    def __init__(self, container_name, crash_schedule, broker_port=1883,
                 port_poll_interval=0.25, port_poll_timeout=30):
        """
        Args:
            container_name: Docker container name to stop/start.
            crash_schedule: List of ``{"time": <float>, "duration": <float>}``
                            dicts sorted by ascending time.  Times are in
                            seconds relative to measurement start.
            broker_port: TCP port to poll for recovery verification.
            port_poll_interval: Seconds between recovery polls.
            port_poll_timeout: Max seconds to wait for port after docker start.
        """
        self.container_name = container_name
        self.crash_schedule = sorted(crash_schedule, key=lambda c: c["time"])
        self.broker_port = broker_port
        self.port_poll_interval = port_poll_interval
        self.port_poll_timeout = port_poll_timeout

        self.crash_events: list[CrashEvent] = []
        self._thread = None
        self._stop_event = threading.Event()
        self._measurement_start = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, measurement_start_time: float):
        """Launch the crash schedule in a daemon thread.

        Args:
            measurement_start_time: ``time.time()`` value marking T=0 for
                                    the crash schedule.
        """
        self._measurement_start = measurement_start_time
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="CrashInjector")
        self._thread.start()
        logger.info("CrashInjector started – %d crash(es) scheduled",
                    len(self.crash_schedule))

    def stop(self):
        """Signal the injector to stop and wait for the thread to finish."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)

    def get_crash_events(self) -> list[dict]:
        """Return completed crash events as serialisable dicts."""
        return [e.to_dict() for e in self.crash_events]

    def get_summary(self) -> dict:
        """Return aggregate crash-recovery statistics."""
        completed = [e for e in self.crash_events if e.success]
        failed = [e for e in self.crash_events if not e.success]

        avg_downtime = 0.0
        avg_recovery_latency = 0.0
        if completed:
            avg_downtime = sum(e.actual_downtime for e in completed) / len(completed)
            avg_recovery_latency = sum(e.recovery_latency for e in completed) / len(completed)

        total_downtime = sum(e.actual_downtime for e in completed)

        return {
            "total_crashes_scheduled": len(self.crash_schedule),
            "total_crashes_executed": len(self.crash_events),
            "successful_recoveries": len(completed),
            "failed_recoveries": len(failed),
            "avg_downtime_sec": round(avg_downtime, 2),
            "avg_recovery_latency_sec": round(avg_recovery_latency, 2),
            "total_downtime_sec": round(total_downtime, 2),
            "crash_events": self.get_crash_events(),
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run(self):
        """Execute the crash schedule sequentially."""
        try:
            client = docker.from_env()
        except Exception as exc:
            logger.error("CrashInjector cannot connect to Docker: %s", exc)
            return

        for entry in self.crash_schedule:
            if self._stop_event.is_set():
                logger.info("CrashInjector stopped early")
                break

            target_time = entry["time"]
            duration = entry["duration"]
            event = CrashEvent(target_time, duration)

            # --- Wait until crash time ---------------------------------
            now = time.time()
            wait_seconds = (self._measurement_start + target_time) - now
            if wait_seconds > 0:
                logger.info("CrashInjector waiting %.1fs until T=%.1fs",
                            wait_seconds, target_time)
                if self._stop_event.wait(timeout=wait_seconds):
                    break  # stopped early

            # --- Stop container ----------------------------------------
            try:
                container = client.containers.get(self.container_name)
                logger.warning("CRASH INJECTED at T=%.1fs – stopping '%s'",
                               target_time, self.container_name)
                event.actual_crash_time = time.time()
                container.stop(timeout=2)
                logger.info("Container '%s' stopped", self.container_name)
            except Exception as exc:
                event.error = f"Stop failed: {exc}"
                logger.error("CrashInjector stop error: %s", exc)
                self.crash_events.append(event)
                continue

            # --- Wait crash duration -----------------------------------
            if self._stop_event.wait(timeout=duration):
                # Early stop requested – still try to restart the broker
                logger.info("CrashInjector stop requested during crash window, restarting broker")

            # --- Restart container -------------------------------------
            try:
                container = client.containers.get(self.container_name)
                event.container_start_time = time.time()
                container.start()
                logger.info("Container '%s' start issued", self.container_name)
            except Exception as exc:
                event.error = f"Start failed: {exc}"
                logger.error("CrashInjector start error: %s", exc)
                self.crash_events.append(event)
                continue

            # --- Poll port until broker is accepting connections -------
            recovered = self._wait_for_port("localhost", self.broker_port)
            event.actual_recovery_time = time.time()

            if recovered:
                event.actual_downtime = event.actual_recovery_time - event.actual_crash_time
                event.recovery_latency = event.actual_downtime - duration
                event.success = True
                logger.info(
                    "BROKER RECOVERED at T=%.1fs – downtime=%.2fs (scheduled=%.1fs, "
                    "extra=%.2fs)",
                    time.time() - self._measurement_start,
                    event.actual_downtime, duration, event.recovery_latency,
                )
            else:
                event.actual_downtime = event.actual_recovery_time - event.actual_crash_time
                event.recovery_latency = event.actual_downtime - duration
                event.error = "Port did not respond within timeout"
                logger.error("Broker did NOT recover within %ds timeout!",
                             self.port_poll_timeout)

            self.crash_events.append(event)

        logger.info("CrashInjector finished – %d/%d events completed",
                    len(self.crash_events), len(self.crash_schedule))

    def _wait_for_port(self, host, port) -> bool:
        """Poll a TCP port until it accepts a connection or timeout."""
        deadline = time.time() + self.port_poll_timeout
        while time.time() < deadline:
            try:
                sock = socket.create_connection((host, port), timeout=1)
                sock.close()
                return True
            except (ConnectionRefusedError, OSError, socket.timeout):
                time.sleep(self.port_poll_interval)
        return False
