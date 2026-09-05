"""
Echo VR goalie trainer - arena geometry calibration tool.

Pure HTTP polling (no Frida, no rendering) - watches disc.velocity for a
sudden direction reversal (dot product of consecutive velocity vectors
flips strongly negative while speed stays roughly similar - the signature
of a real wall/floor/ceiling bounce, as opposed to just slowing down under
drag or being caught/released) and logs the disc's real position at that
moment. This gives REAL ground-truth coordinates for arena boundaries,
instead of the guessed box dimensions in arena_geometry.py.

Run: python detect_real_bounces.py
Throw the disc at walls, floor, and ceiling deliberately - one at a time,
pause between throws so bounces don't blur together. Watch the printed
output for each detected bounce's position. Ctrl+C to stop.
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "log.txt")
API_URL = "http://127.0.0.1:6721/session"
POLL_INTERVAL_S = 0.02  # ~50Hz - faster than the render-hook script, since
                         # we need fine-grained velocity samples to catch
                         # the exact bounce moment, not just draw a line

# Detection thresholds
MIN_SPEED = 3.0          # ignore near-stationary noise
REVERSAL_DOT_THRESHOLD = -0.3  # normalized dot product of consecutive
                                # velocity directions - a real bounce
                                # flips this strongly negative
RUN_SECONDS = 90          # give enough time to test several walls/floor/ceiling


def normalize(v):
    length = (v[0] ** 2 + v[1] ** 2 + v[2] ** 2) ** 0.5
    if length < 1e-6:
        return (0.0, 0.0, 0.0)
    return (v[0] / length, v[1] / length, v[2] / length)


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


class Tee:
    def __init__(self, filename):
        self.terminal = sys.stdout
        self.log = open(filename, "a", encoding="utf-8", buffering=1)

    def write(self, message):
        self.terminal.write(message)
        self.log.write(message)

    def flush(self):
        self.terminal.flush()
        self.log.flush()

    def close(self):
        self.log.close()


def main():
    tee = Tee(LOG_PATH)
    sys.stdout = tee
    print()
    print("#" * 70)
    print(f"# RUN START: {datetime.now().isoformat(timespec='seconds')}")
    print("#" * 70)
    print()
    try:
        _main_body()
    finally:
        print()
        print(f"# RUN END: {datetime.now().isoformat(timespec='seconds')}")
        print("#" * 70)
        sys.stdout = tee.terminal
        tee.close()


def _main_body():
    print("Watching disc.velocity for real bounces (Ctrl+C to stop)...")
    print("Throw the disc at walls/floor/ceiling one at a time.\n")

    last_vel = None
    last_pos = None
    last_up = None
    last_speed = 0.0
    bounce_count = 0
    start_time = time.monotonic()

    while time.monotonic() - start_time < RUN_SECONDS:
        try:
            req = urllib.request.urlopen(API_URL, timeout=1)
            data = json.loads(req.read().decode())
            disc = data.get("disc", {})
            pos = disc.get("position")
            vel = disc.get("velocity")
            up = disc.get("up")   # disc plane normal - orients the 40-particle
                                  # ring collider in echo_disc_predict

            if pos and vel:
                speed = (vel[0] ** 2 + vel[1] ** 2 + vel[2] ** 2) ** 0.5
                if last_vel is not None and speed > MIN_SPEED and last_speed > MIN_SPEED:
                    d = dot(normalize(vel), normalize(last_vel))
                    if d < REVERSAL_DOT_THRESHOLD:
                        bounce_count += 1
                        ts = datetime.now().isoformat(timespec="milliseconds")
                        print(f"[{ts}] BOUNCE #{bounce_count} detected!")
                        print(f"    position ~ {last_pos}")
                        print(f"    velocity before: {last_vel} (speed={last_speed:.2f})")
                        print(f"    velocity after:  {vel} (speed={speed:.2f})")
                        print(f"    disc up: {last_up}")
                        print(f"    direction dot product: {d:.3f}\n")

                last_vel = vel
                last_pos = pos
                last_up = up
                last_speed = speed

        except KeyboardInterrupt:
            break
        except Exception:
            pass

        time.sleep(POLL_INTERVAL_S)

    print(f"\nTotal bounces detected: {bounce_count}")


if __name__ == "__main__":
    main()
