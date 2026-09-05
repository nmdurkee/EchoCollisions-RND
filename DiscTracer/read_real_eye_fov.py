"""
Echo VR renderer-hook research - LIVE, READ-ONLY: read the REAL per-eye
FOV values (ovrFovPort: UpTan, DownTan, LeftTan, RightTan) directly from
the global memory locations found via static analysis of the decompile
(echovr.exe_CLINET.c, FUN_14072d110 - the function that computes the
recommended eye-buffer texture size, confirmed correct because its
hardcoded 1344x1600 fallback matches the CONFIRMED real headset target
buffer exactly).

  LEFT eye ovrFovPort:  base + 0xC7860  (16 bytes: 4 floats)
  RIGHT eye ovrFovPort: base + 0xC7870  (16 bytes: 4 floats)

These are cached once at OVR init and never move again for the session,
so a single plain memory read is all that's needed - no hooking, no
timing issues, matches this project's proven "plain reads always work"
discipline. Zero writes.

Run: python read_real_eye_fov.py
"""

import frida
import sys
import os
from datetime import datetime

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "log.txt")

LEFT_FOV_RVA = 0x20C7860
RIGHT_FOV_RVA = 0x20C7870

JS_SCRIPT = r"""
var base = Process.getModuleByName("echovr.exe").base;
var leftPtr = base.add(LEFT_RVA_PLACEHOLDER);
var rightPtr = base.add(RIGHT_RVA_PLACEHOLDER);

function readFovPort(p) {
    return {
        upTan: p.add(0).readFloat(),
        downTan: p.add(4).readFloat(),
        leftTan: p.add(8).readFloat(),
        rightTan: p.add(12).readFloat()
    };
}

var left = readFovPort(leftPtr);
var right = readFovPort(rightPtr);

send({type: "result", msg: "LEFT eye FOV: " + JSON.stringify(left)});
send({type: "result", msg: "RIGHT eye FOV: " + JSON.stringify(right)});

rpc.exports.getFov = function() {
    return { left: left, right: right };
};
""".replace("LEFT_RVA_PLACEHOLDER", hex(LEFT_FOV_RVA)).replace("RIGHT_RVA_PLACEHOLDER", hex(RIGHT_FOV_RVA))


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


def on_message(message, data):
    if message["type"] == "send":
        p = message["payload"]
        if isinstance(p, dict):
            print(f">>> [{p.get('type','')}] {p.get('msg','')}")
    elif message["type"] == "error":
        print("FRIDA ERROR: " + message.get("stack", ""))


def main():
    tee = Tee(LOG_PATH)
    sys.stdout = tee
    print()
    print("#" * 70)
    print(f"# RUN START: {datetime.now().isoformat(timespec='seconds')}")
    print("#" * 70)
    print()
    print("LIVE read: real per-eye FOV from echovr.exe global memory (READ-ONLY)")
    print("=" * 60)
    try:
        _main_body()
    finally:
        print()
        print(f"# RUN END: {datetime.now().isoformat(timespec='seconds')}")
        print("#" * 70)
        sys.stdout = tee.terminal
        tee.close()


def _main_body():
    try:
        session = frida.attach("echovr.exe")
    except frida.ProcessNotFoundError:
        print("ERROR: echovr.exe not found")
        sys.exit(1)

    script = session.create_script(JS_SCRIPT)
    script.on("message", on_message)
    script.load()

    try:
        fov = script.exports_sync.get_fov()
        print(f"\n[*] LEFT FOV tangents: {fov['left']}")
        print(f"[*] RIGHT FOV tangents: {fov['right']}")
        import math
        for name, f in [("LEFT", fov['left']), ("RIGHT", fov['right'])]:
            up_deg = math.degrees(math.atan(f['upTan']))
            down_deg = math.degrees(math.atan(f['downTan']))
            left_deg = math.degrees(math.atan(f['leftTan']))
            right_deg = math.degrees(math.atan(f['rightTan']))
            print(f"[*] {name} eye FOV in degrees: up={up_deg:.2f} down={down_deg:.2f} left={left_deg:.2f} right={right_deg:.2f} (h-total={left_deg+right_deg:.2f} v-total={up_deg+down_deg:.2f})")
    except Exception as e:
        print(f"[*] get_fov failed: {e}")

    print("\n[*] Done (read-only, zero writes made).")
    try:
        session.detach()
    except Exception:
        pass


if __name__ == "__main__":
    main()
