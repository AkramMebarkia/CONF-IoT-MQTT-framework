"""
Clock synchronization utilities for distributed testing.

Uses Cristian's algorithm to estimate clock offset between Flask server and Node-RED.
"""
import logging
import statistics
import time
import requests

logger = logging.getLogger(__name__)


def estimate_clock_offset(node_red_url: str, samples: int = 10) -> float:
    """
    Estimate clock offset between Flask server and Node-RED using Cristian's algorithm.
    
    This sends multiple timestamp requests to Node-RED and estimates the difference
    between local time and Node-RED time, accounting for network round-trip time.
    
    Args:
        node_red_url: Base URL of Node-RED (e.g., "http://localhost:1880")
        samples: Number of samples to take for more accurate estimation
    
    Returns:
        Offset in milliseconds (positive = Node-RED ahead, negative = Flask ahead)
        Returns 0.0 if estimation fails.
    
    Notes:
        - Requires a /clock endpoint in Node-RED that returns {"time": Date.now()}
        - Uses median to filter out outliers from network jitter
    """
    offsets = []
    
    for i in range(samples):
        try:
            t1 = time.time() * 1000  # Flask time before request (ms)
            resp = requests.get(f"{node_red_url}/clock", timeout=2)
            t2 = time.time() * 1000  # Flask time after response (ms)
            
            if resp.status_code == 200:
                data = resp.json()
                node_red_time = data.get('time', 0)
                
                rtt = t2 - t1
                estimated_flask_time_at_nodered = t1 + rtt / 2
                offset = node_red_time - estimated_flask_time_at_nodered
                offsets.append(offset)
                
                logger.debug("Clock sample %d: RTT=%.1fms, offset=%.1fms", i, rtt, offset)
        except requests.RequestException as e:
            logger.debug("Clock sync sample %d failed: %s", i, e)
        except Exception as e:
            logger.warning("Clock sync error: %s", e)
        
        time.sleep(0.1)
    
    if not offsets:
        logger.warning("Clock sync failed after %d attempts, assuming zero offset", samples)
        return 0.0
    
    median_offset = statistics.median(offsets)
    std_dev = statistics.stdev(offsets) if len(offsets) > 1 else 0
    
    logger.info("Clock offset estimated: %.2fms (std=%.2fms, samples=%d)", 
               median_offset, std_dev, len(offsets))
    
    return median_offset


def apply_clock_offset(latency_ms: float, clock_offset: float) -> float:
    """
    Apply clock offset correction to a latency measurement.
    
    Args:
        latency_ms: Raw latency in milliseconds (receiver_time - sender_time)
        clock_offset: Offset in milliseconds (sender_clock - receiver_clock)
    
    Returns:
        Corrected latency in milliseconds
    """
    corrected = latency_ms - clock_offset
    return max(0, corrected)  # Latency cannot be negative
