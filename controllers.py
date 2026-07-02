"""Controller abstraction for aigp-manual-fly.

Backends:
  1. pygame (SDL2) — preferred: sees every HID joystick Windows knows about,
     including RC transmitters in USB-joystick mode (EdgeTX/OpenTX radios),
     Xbox/XInput pads, PlayStation pads, and flight sticks.
  2. XInput via ctypes — zero-dependency fallback for Xbox-style pads when
     pygame is not installed.

Mapping resolution order for a detected device:
  saved user profile (from --setup wizard, keyed by device GUID/name)
  > built-in preset matched on device name
  > generic default (run --setup!).
"""
import ctypes
import json
import os
import re
import sys
import time

PROFILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles.json")

# ---------------------------------------------------------------------------
# Built-in presets. Axis directions follow SDL conventions (stick up = -1 on
# Y axes for gamepads). "thrust_full_range": True means the throttle control
# holds its position across the full travel (RC transmitter / slider);
# False means it springs back to center (gamepad stick) and only the upper
# half is used unless the hover-centered curve is active.
# ---------------------------------------------------------------------------
PRESETS = [
    {
        "name": "RC transmitter (EdgeTX / OpenTX / FrSky / Spektrum, AETR)",
        "match": r"edgetx|opentx|radiomaster|taranis|frsky|jumper|betafpv|tbs|spektrum|interlink|flysky|eachine",
        "axes": {"roll": 0, "pitch": 1, "thrust": 2, "yaw": 3},   # AETR channel order
        "invert": {"roll": False, "pitch": False, "yaw": False, "thrust": False},
        "thrust_full_range": True,
        "buttons": {"enable": 0, "cut": 1},
    },
    {
        "name": "Xbox / XInput gamepad (Mode 2)",
        "match": r"xbox|xinput|x360|360 controller|f310|f510|f710|one controller|series controller",
        "axes": {"yaw": 0, "thrust": 1, "roll": 2, "pitch": 3},
        "invert": {"roll": False, "pitch": False, "yaw": False, "thrust": True},
        "thrust_full_range": False,
        "buttons": {"enable": 0, "cut": 1},                        # A / B
    },
    {
        "name": "PlayStation controller (Mode 2)",
        "match": r"playstation|dualsense|dualshock|ps4|ps5|wireless controller",
        "axes": {"yaw": 0, "thrust": 1, "roll": 2, "pitch": 3},
        "invert": {"roll": False, "pitch": False, "yaw": False, "thrust": True},
        "thrust_full_range": False,
        "buttons": {"enable": 0, "cut": 1},                        # cross / circle
    },
    {
        "name": "Logitech Extreme 3D Pro (twist yaw, slider throttle)",
        "match": r"extreme\s*3d",
        "axes": {"roll": 0, "pitch": 1, "yaw": 2, "thrust": 3},
        "invert": {"roll": False, "pitch": False, "yaw": False, "thrust": True},
        "thrust_full_range": True,
        "buttons": {"enable": 0, "cut": 1},                        # trigger / thumb
    },
    {
        "name": "Thrustmaster / generic flight stick",
        "match": r"thrustmaster|t\.16000|t16000|hotas|flight\s*stick",
        "axes": {"roll": 0, "pitch": 1, "yaw": 2, "thrust": 3},
        "invert": {"roll": False, "pitch": False, "yaw": False, "thrust": True},
        "thrust_full_range": True,
        "buttons": {"enable": 0, "cut": 1},
    },
]

GENERIC = {
    "name": "generic (unmapped — run --setup)",
    "axes": {"roll": 0, "pitch": 1, "yaw": 3, "thrust": 2},
    "invert": {"roll": False, "pitch": False, "yaw": False, "thrust": False},
    "thrust_full_range": True,
    "buttons": {"enable": 0, "cut": 1},
}


def load_profiles():
    try:
        with open(PROFILE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_profile(key, profile):
    profiles = load_profiles()
    profiles[key] = profile
    with open(PROFILE_PATH, "w", encoding="utf-8") as f:
        json.dump(profiles, f, indent=2)


def match_preset(device_name):
    for p in PRESETS:
        if re.search(p["match"], device_name, re.IGNORECASE):
            return p
    return None


# ---------------------------------------------------------------------------
# pygame backend
# ---------------------------------------------------------------------------
class PygameBackend:
    def __init__(self, index=None):
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
        import pygame
        self.pygame = pygame
        pygame.init()
        pygame.joystick.init()
        count = pygame.joystick.get_count()
        if count == 0:
            raise RuntimeError("no joystick detected")
        self.js = pygame.joystick.Joystick(index if index is not None else 0)
        self.name = self.js.get_name()
        try:
            self.key = self.js.get_guid()
        except Exception:
            self.key = self.name

    @staticmethod
    def list_devices():
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
        import pygame
        pygame.init()
        pygame.joystick.init()
        out = []
        for i in range(pygame.joystick.get_count()):
            js = pygame.joystick.Joystick(i)
            out.append((i, js.get_name(), js.get_numaxes(), js.get_numbuttons()))
        return out

    def poll(self):
        try:
            self.pygame.event.pump()
            if self.pygame.joystick.get_count() == 0:
                return None, None   # unplugged or went to sleep
            axes = [self.js.get_axis(i) for i in range(self.js.get_numaxes())]
            buttons = [bool(self.js.get_button(i)) for i in range(self.js.get_numbuttons())]
            return axes, buttons
        except self.pygame.error:
            return None, None

    def connected(self):
        return self.pygame.joystick.get_count() > 0


# ---------------------------------------------------------------------------
# XInput fallback backend (Xbox-style pads only, Windows only, no deps)
# ---------------------------------------------------------------------------
class _XGamepad(ctypes.Structure):
    _fields_ = [("wButtons", ctypes.c_ushort), ("bLT", ctypes.c_ubyte), ("bRT", ctypes.c_ubyte),
                ("lx", ctypes.c_short), ("ly", ctypes.c_short),
                ("rx", ctypes.c_short), ("ry", ctypes.c_short)]


class _XState(ctypes.Structure):
    _fields_ = [("pkt", ctypes.c_uint), ("g", _XGamepad)]


class XInputBackend:
    """Presents XInput as SDL-style axes [LX, LY, RX, RY] + buttons [A,B,X,Y,...]."""
    BUTTON_ORDER = [0x1000, 0x2000, 0x4000, 0x8000, 0x0100, 0x0200, 0x0020, 0x0010]

    def __init__(self, index=None):
        self.dll = None
        for name in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
            try:
                self.dll = getattr(ctypes.windll, name)
                break
            except OSError:
                continue
        if self.dll is None:
            raise RuntimeError("XInput not available")
        self.slot = None
        for i in ([index] if index is not None else range(4)):
            if self._read(i) is not None:
                self.slot = i
                break
        if self.slot is None:
            raise RuntimeError("no XInput controller detected")
        self.name = "XInput gamepad (slot %d)" % self.slot
        self.key = "xinput"

    def _read(self, slot):
        s = _XState()
        if self.dll.XInputGetState(slot, ctypes.byref(s)) != 0:
            return None
        return s.g

    def poll(self):
        g = self._read(self.slot)
        if g is None:
            return None, None
        n = lambda v: max(-1.0, min(1.0, v / 32767.0))
        # SDL convention: stick up = negative Y
        axes = [n(g.lx), -n(g.ly), n(g.rx), -n(g.ry)]
        buttons = [bool(g.wButtons & m) for m in self.BUTTON_ORDER]
        return axes, buttons

    def connected(self):
        return self._read(self.slot) is not None


# ---------------------------------------------------------------------------
# Controller: backend + mapping -> named channels
# ---------------------------------------------------------------------------
class Controller:
    def __init__(self, index=None, force_profile=None):
        try:
            self.backend = PygameBackend(index)
        except ImportError:
            print("(pygame not installed — falling back to XInput; RC transmitters "
                  "and non-Xbox devices need: pip install pygame-ce)")
            self.backend = XInputBackend(index)
        except RuntimeError:
            self.backend = XInputBackend(index)   # pygame present but saw nothing

        self.name = self.backend.name
        profiles = load_profiles()
        if force_profile is not None:
            self.profile = force_profile
            self.source = "wizard"
        elif self.backend.key in profiles:
            self.profile = profiles[self.backend.key]
            self.source = "saved profile"
        else:
            preset = match_preset(self.name)
            self.profile = preset if preset else GENERIC
            self.source = "preset: " + self.profile["name"] if preset else "GENERIC GUESS — run --setup"

    def read(self):
        """Returns (channels dict with roll/pitch/yaw in -1..1 and thrust_raw in -1..1,
        buttons dict enable/cut pressed, ok flag)."""
        axes, buttons = self.backend.poll()
        if axes is None:
            return None, None, False
        p = self.profile

        def ax(namech):
            i = p["axes"][namech]
            v = axes[i] if 0 <= i < len(axes) else 0.0
            return -v if p["invert"][namech] else v

        chans = {"roll": ax("roll"), "pitch": ax("pitch"), "yaw": ax("yaw"),
                 "thrust_raw": ax("thrust"), "full_range": p.get("thrust_full_range", True)}

        def btn(k):
            i = p["buttons"].get(k, -1)
            return bool(buttons[i]) if 0 <= i < len(buttons) else False

        return chans, {"enable": btn("enable"), "cut": btn("cut")}, True


# ---------------------------------------------------------------------------
# Interactive calibration wizard (--setup)
# ---------------------------------------------------------------------------
def _sample(backend, seconds):
    t_end = time.time() + seconds
    frames = []
    while time.time() < t_end:
        axes, buttons = backend.poll()
        if axes is not None:
            frames.append((list(axes), list(buttons)))
        time.sleep(0.01)
    return frames


def _detect_moved_axis(backend, rest, prompt):
    input(f"\n{prompt}\n  ...then press Enter and HOLD the position: ")
    frames = _sample(backend, 1.2)
    best_axis, best_delta = None, 0.25   # require real deflection
    for axes, _ in frames:
        for i, v in enumerate(axes):
            d = v - rest[i]
            if abs(d) > abs(best_delta) or (best_axis is None and abs(d) > 0.25):
                if abs(d) > abs(best_delta):
                    best_axis, best_delta = i, d
    if best_axis is None:
        print("  !! no axis moved — skipping (edit profiles.json manually if needed)")
        return None, 1.0
    print(f"  -> axis {best_axis} (deflection {best_delta:+.2f})")
    return best_axis, best_delta


def _detect_pressed_button(backend, prompt):
    input(f"\n{prompt}\n  ...then press Enter and HOLD it: ")
    frames = _sample(backend, 1.2)
    for _, buttons in frames:
        for i, b in enumerate(buttons):
            if b:
                print(f"  -> button {i}")
                return i
    print("  !! no button detected — keyboard keys will still work")
    return -1


def run_wizard(index=None):
    backend = None
    try:
        backend = PygameBackend(index)
    except (ImportError, RuntimeError):
        backend = XInputBackend(index)
    print(f"\nCalibrating: {backend.name}")
    print("Leave all sticks/switches at REST (throttle fully DOWN if it holds position).")
    input("Press Enter when ready: ")
    frames = _sample(backend, 1.0)
    rest = [sum(f[0][i] for f in frames) / len(frames) for i in range(len(frames[0][0]))]

    profile = {"name": "custom: " + backend.name,
               "axes": {}, "invert": {}, "buttons": {}, "thrust_full_range": True}

    for ch, prompt in [
        ("thrust", "Move THROTTLE fully UP / forward"),
        ("yaw",    "Move YAW fully RIGHT"),
        ("pitch",  "Move PITCH fully FORWARD (away from you)"),
        ("roll",   "Move ROLL fully RIGHT"),
    ]:
        axis, delta = _detect_moved_axis(backend, rest, prompt)
        if axis is None:
            axis, delta = GENERIC["axes"][ch], 1.0
        profile["axes"][ch] = axis
        if ch == "thrust":
            # want thrust up => +1: invert if up-deflection is negative
            profile["invert"][ch] = delta < 0
            # springy stick rests near center; held throttle rests near an end
            profile["thrust_full_range"] = abs(rest[axis]) > 0.5
        elif ch == "pitch":
            # convention: stick forward = nose DOWN = negative pitch command
            profile["invert"][ch] = delta > 0
        else:
            # right = positive
            profile["invert"][ch] = delta < 0

    profile["buttons"]["enable"] = _detect_pressed_button(
        backend, "Press the button/switch you want as ENABLE CONTROL (arm)")
    profile["buttons"]["cut"] = _detect_pressed_button(
        backend, "Press the button/switch you want as CUT CONTROL (kill)")

    save_profile(backend.key, profile)
    print(f"\nSaved to {PROFILE_PATH} (device key: {backend.key})")
    print(json.dumps(profile, indent=2))
    return profile
