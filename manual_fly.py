#!/usr/bin/env python3
"""aigp-manual-fly: fly the AI-GP FlightSim by hand with any joystick.

Reads your controller (RC transmitter in USB-joystick mode, Xbox/PlayStation
pad, or flight stick) and streams SET_ATTITUDE_TARGET body-rate + thrust
commands to the simulator's local MAVLink UDP interface — the same interface
your autonomous pilot uses.

  *** TRAINING EVENTS ONLY ***
  Human input during a SUBMISSION run is an immediate DSQ, and run records
  upload automatically. Always select the "... - TRAINING" event.

Quick start:
  fly_manual.bat --setup     first run: interactive stick calibration
  fly_manual.bat             fly (defaults are gentle trainer rates)

Controls:
  enable button (or 'a' key)   start sending commands — press AFTER "GO"
  cut button    (or space/'b') stop sending immediately
  'q' / Ctrl+C                 quit

Sim contract: client binds udpin:127.0.0.1:14550 (sim sends from 14560),
SET_ATTITUDE_TARGET body rates rad/s + thrust 0..1 at < 100 Hz, HEARTBEAT
>= 2 Hz, ACRO only (rate control, no self-leveling).
"""
import argparse
import math
import sys
import time

import controllers

try:
    import msvcrt  # Windows console keys; the sim is Windows-only anyway
except ImportError:
    msvcrt = None


def shape(v, deadband, expo):
    if abs(v) < deadband:
        return 0.0
    v = (v - math.copysign(deadband, v)) / (1.0 - deadband)
    return (1.0 - expo) * v + expo * v ** 3


def thrust_from_stick(raw, full_range, hover, max_thrust, linear):
    """raw is -1..1 with +1 = full throttle (profile already fixed direction)."""
    if full_range or linear:
        t = (raw + 1.0) / 2.0
        return min(max_thrust, max(0.0, t))
    # springy gamepad stick: center = hover, up = max, down = zero
    if raw >= 0:
        return hover + raw * (max_thrust - hover)
    return max(0.0, hover + raw * hover)


def main():
    ap = argparse.ArgumentParser(
        description="Manual MAVLink flight for the AI-GP FlightSim (TRAINING only)")
    ap.add_argument("--port", type=int, default=14550, help="local UDP port to bind (default 14550)")
    ap.add_argument("--rate-roll", type=float, default=70.0, help="max roll deg/s at full stick (racing: 540)")
    ap.add_argument("--rate-pitch", type=float, default=70.0, help="max pitch deg/s at full stick (racing: 540)")
    ap.add_argument("--rate-yaw", type=float, default=50.0, help="max yaw deg/s at full stick (racing: 360)")
    ap.add_argument("--rate-rp", type=float, default=None, help="set roll AND pitch together")
    ap.add_argument("--expo", type=float, default=0.45, help="stick expo 0..0.8 (more = softer center)")
    ap.add_argument("--deadband", type=float, default=0.06, help="stick deadband")
    ap.add_argument("--hover", type=float, default=0.18, help="springy-stick center thrust (~hover)")
    ap.add_argument("--max-thrust", type=float, default=1.0, help="thrust ceiling 0..1")
    ap.add_argument("--linear-throttle", action="store_true",
                    help="force linear 0..1 throttle even on a springy stick")
    ap.add_argument("--pitch-fwd-up", action="store_true", help="stick forward = nose UP")
    ap.add_argument("--hz", type=float, default=50.0, help="command rate (keep < 100)")
    ap.add_argument("--device", type=int, default=None, help="joystick index (see --list)")
    ap.add_argument("--list", action="store_true", help="list detected joysticks and exit")
    ap.add_argument("--setup", action="store_true", help="run the stick calibration wizard")
    ap.add_argument("--test", action="store_true", help="print stick values only, no MAVLink")
    args = ap.parse_args()
    if args.rate_rp is not None:
        args.rate_roll = args.rate_pitch = args.rate_rp

    if args.list:
        try:
            for i, name, naxes, nbtn in controllers.PygameBackend.list_devices():
                print(f"  [{i}] {name}  ({naxes} axes, {nbtn} buttons)")
        except ImportError:
            print("pygame not installed; only XInput pads visible. pip install pygame-ce")
        return

    if args.setup:
        controllers.run_wizard(args.device)
        return

    print(__doc__.split("Quick start:")[0])
    ctl = None
    waiting_shown = False
    while ctl is None:
        try:
            ctl = controllers.Controller(args.device)
        except RuntimeError:
            if not waiting_shown:
                print("No controller detected — waiting. Press a button on it to wake it "
                      "(transmitters: enable USB joystick mode). Ctrl+C to quit.")
                waiting_shown = True
            time.sleep(1.5)
    print(f"Controller: {ctl.name}   [{ctl.source}]")
    print(f"Rates deg/s: roll {args.rate_roll:.0f} / pitch {args.rate_pitch:.0f} / "
          f"yaw {args.rate_yaw:.0f}   expo {args.expo}   max thrust {args.max_thrust}")
    if "GENERIC" in ctl.source:
        print(">> Unknown device: mapping is a guess. Run with --setup to calibrate! <<")

    conn = None
    if not args.test:
        from pymavlink import mavutil
        conn = mavutil.mavlink_connection(f"udpin:127.0.0.1:{args.port}", source_system=255)
        print(f"\nListening on udp 127.0.0.1:{args.port} — start FlightSim and "
              f"select a TRAINING event...")
        conn.wait_heartbeat()
        print(f"Sim connected (system {conn.target_system}). Click RACE; after GO, "
              f"press your enable button (or 'a').")

    enabled = False
    prev = {"enable": False, "cut": False}
    period = 1.0 / args.hz
    hb_next = 0.0
    status_next = 0.0
    d2r = math.pi / 180.0
    t0 = time.time()

    while True:
        loop_start = time.time()

        if msvcrt and msvcrt.kbhit():
            k = msvcrt.getch().lower()
            if k == b"q":
                print("\nQuit.")
                return
            if k == b"a":
                enabled = True
                print("\n>>> CONTROL ENABLED (keyboard)")
            if k in (b"b", b" "):
                enabled = False
                print("\n>>> CONTROL CUT (keyboard)")

        chans, btns, ok = ctl.read()
        if not ok:
            if enabled:
                print("\nController lost (asleep/unplugged) — control CUT. Press a button on it...")
            enabled = False
            time.sleep(0.5)
            try:   # re-acquire (wake from sleep gets a fresh device instance)
                ctl = controllers.Controller(args.device)
            except RuntimeError:
                pass
            continue

        if btns["enable"] and not prev["enable"]:
            enabled = True
            print("\n>>> CONTROL ENABLED")
        if btns["cut"] and not prev["cut"]:
            enabled = False
            print("\n>>> CONTROL CUT")
        prev = btns

        roll_in = shape(chans["roll"], args.deadband, args.expo)
        pitch_in = shape(chans["pitch"], args.deadband, args.expo)
        yaw_in = shape(chans["yaw"], args.deadband, args.expo)

        # NED body rates: +roll = right bank, +pitch = nose up, +yaw = nose right.
        # Profiles put pitch stick-forward at negative (= nose down) already.
        pitch_sign = -1.0 if args.pitch_fwd_up else 1.0
        roll_rate = roll_in * args.rate_roll * d2r
        pitch_rate = pitch_sign * pitch_in * args.rate_pitch * d2r
        yaw_rate = yaw_in * args.rate_yaw * d2r
        thrust = thrust_from_stick(chans["thrust_raw"], chans["full_range"],
                                   args.hover, args.max_thrust,
                                   args.linear_throttle) if enabled else 0.0

        now = time.time()
        if conn is not None:
            if enabled:
                conn.mav.set_attitude_target_send(
                    int((now - t0) * 1000) & 0xFFFFFFFF,
                    conn.target_system, conn.target_component,
                    128,                       # ignore attitude quat; body rates + thrust
                    [1.0, 0.0, 0.0, 0.0],
                    roll_rate, pitch_rate, yaw_rate, thrust)
            if now >= hb_next:
                conn.mav.heartbeat_send(mavutil.mavlink.MAV_TYPE_GCS,
                                        mavutil.mavlink.MAV_AUTOPILOT_INVALID, 0, 0, 0)
                hb_next = now + 0.5
            while conn.recv_match(blocking=False) is not None:
                pass                           # drain socket

        if now >= status_next:
            state = "FLY" if enabled else "standby — press enable AFTER GO"
            print(f"\r[{state}]  thr {thrust:5.2f}  roll {roll_in:+.2f}  "
                  f"pitch {pitch_in:+.2f}  yaw {yaw_in:+.2f}    ", end="", flush=True)
            status_next = now + 0.2

        sleep = period - (time.time() - loop_start)
        if sleep > 0:
            time.sleep(sleep)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nQuit.")
