#!/usr/bin/env python3
"""
Test Art-Net source: sends a running rainbow to RGB chips at 60 packets/s and prints a log.

  python packets_tester.py                          -> 127.0.0.1:6454, 104 chips, universe 1
  python packets_tester.py 127.0.0.1 6454 104 1     -> host port chips universe

Note: UDP is connectionless, so this script cannot know whether anybody is listening.
Check the statistics block of the receiving application to confirm that packets arrive.
"""

import argparse
import colorsys
import math
import socket
import sys
import time

RATE = 60.0  # packets per second
PERIOD = 6.0  # animation period in seconds (seamless loop)
LOG_EVERY = 1.0  # seconds between status lines


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def make_packet(universe, seq, dmx):
    """Build an ArtDMX packet (OpCode 0x5000, protocol version 14)."""
    dmx = bytes(dmx) + b"\x00" * (len(dmx) % 2)
    header = b"Art-Net\x00" + bytes(
        [0x00, 0x50, 0, 14, seq & 0xFF, 0, universe & 0xFF, (universe >> 8) & 0x7F]
    )
    return header + bytes([len(dmx) >> 8, len(dmx) & 0xFF]) + dmx


def rainbow(t, chips):
    dmx = bytearray(512)
    for c in range(chips):
        hue = (t / PERIOD + c / chips) % 1.0
        val = 0.6 + 0.4 * math.sin(2 * math.pi * (t / PERIOD * 2 + c / chips)) ** 2
        r, g, b = colorsys.hsv_to_rgb(hue, 1.0, val)
        dmx[c * 3 : c * 3 + 3] = bytes([int(r * 255), int(g * 255), int(b * 255)])
    return dmx


def main():
    ap = argparse.ArgumentParser(description="Test Art-Net packet source with a log.")
    ap.add_argument("host", nargs="?", default="127.0.0.1")
    ap.add_argument("port", nargs="?", type=int, default=6454)
    ap.add_argument("chips", nargs="?", type=int, default=104)
    ap.add_argument("universe", nargs="?", type=int, default=1)
    args = ap.parse_args()

    if not 1 <= args.port <= 65535:
        log("ERROR: port must be between 1 and 65535")
        return 2
    if not 1 <= args.chips <= 170:
        log("ERROR: chips must be between 1 and 170 (one universe of RGB chips)")
        return 2
    if not 0 <= args.universe <= 32767:
        log("ERROR: universe must be between 0 and 32767")
        return 2
    try:
        ip = socket.gethostbyname(args.host)
    except OSError as e:
        log("ERROR: cannot resolve host '%s': %s" % (args.host, e))
        return 1
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError as e:
        log("ERROR: cannot create the UDP socket: %s" % e)
        return 1

    log(
        "Target %s:%d | universe %d | %d chips (%d channels) | %.0f packets/s | loop %.1f s"
        % (ip, args.port, args.universe, args.chips, args.chips * 3, RATE, PERIOD)
    )
    log("Sending... press Ctrl+C to stop.")

    t0 = time.perf_counter()
    n = seq = total = errors = 0
    win_sent = win_errors = 0
    win_start = t0
    last_error = ""
    try:
        while True:
            t = time.perf_counter() - t0
            packet = make_packet(args.universe, seq, rainbow(t, args.chips))
            try:
                sock.sendto(packet, (ip, args.port))
                total += 1
                win_sent += 1
            except OSError as e:
                errors += 1
                win_errors += 1
                if str(e) != last_error:  # print every new kind of error immediately
                    last_error = str(e)
                    log("ERROR: send failed: %s" % e)
            seq = (seq + 1) % 256
            n += 1
            now = time.perf_counter()
            if now - win_start >= LOG_EVERY:
                log(
                    "sent %d packets (%.1f pkt/s) | total %d | send errors %d (+%d)"
                    % (
                        win_sent,
                        win_sent / (now - win_start),
                        total,
                        errors,
                        win_errors,
                    )
                )
                win_sent = win_errors = 0
                win_start = now
            nxt = t0 + n / RATE
            while time.perf_counter() < nxt:
                time.sleep(0.001)
    except KeyboardInterrupt:
        log("Stopped. Total packets sent: %d, send errors: %d" % (total, errors))
        return 0
    except Exception as e:  # unexpected problem: report it instead of closing silently
        log("FATAL: %s: %s" % (type(e).__name__, e))
        return 1
    finally:
        sock.close()


if __name__ == "__main__":
    sys.exit(main())
