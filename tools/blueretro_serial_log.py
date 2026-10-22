#!/usr/bin/env python3

import argparse
import datetime
import sys

import serial
from serial.tools import list_ports


def pick_port() -> str | None:
    ports = list(list_ports.comports())
    if not ports:
        print("No serial ports found. Is the ESP32 plugged in over USB?")
        return None
    print("Serial ports:")
    for p in ports:
        print(f"  {p.device:8s} {p.description}  [{p.hwid}]")
    for p in ports:
        if any(k in (p.description + p.hwid).upper()
               for k in ("CP210", "CH340", "CH910", "FTDI", "USB SERIAL", "VID:PID=10C4", "VID:PID=1A86")):
            print(f"Using {p.device}")
            return p.device
    usb_ports = [p for p in ports if "USB" in p.hwid.upper()]
    if len(usb_ports) == 1:
        print(f"Using {usb_ports[0].device}")
        return usb_ports[0].device
    if not usb_ports:
        print("No USB serial port found. Is the ESP32 plugged in over USB?")
    else:
        print("Several USB ports found; pass the right one, e.g.: python blueretro_serial_log.py COM5")
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Log BlueRetro's serial debug output")
    parser.add_argument("port", nargs="?", help="serial port, e.g. COM5")
    parser.add_argument("--baud", type=int, default=921600, help="baud rate (default 921600)")
    parser.add_argument("--out", help="log file (default blueretro_<time>.log)")
    args = parser.parse_args()

    port = args.port or pick_port()
    if not port:
        sys.exit(1)
    out = args.out or datetime.datetime.now().strftime("blueretro_%Y%m%d_%H%M%S.log")

    ser = serial.Serial()
    ser.port = port
    ser.baudrate = args.baud
    ser.timeout = 0.2
    ser.dtr = False
    ser.rts = False
    ser.open()
    print(f"Logging {port} @ {args.baud} to {out}  (Ctrl+C to stop)\n")

    buf = b""
    with open(out, "a", encoding="utf-8", errors="replace") as f:
        try:
            while True:
                buf += ser.read(4096)
                *lines, buf = buf.split(b"\n")
                for raw in lines:
                    text = raw.rstrip(b"\r").decode("utf-8", errors="replace")
                    stamp = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
                    line = f"{stamp} {text}"
                    print(line)
                    f.write(line + "\n")
                f.flush()
        except KeyboardInterrupt:
            print(f"\nSaved to {out}")
        finally:
            ser.close()


if __name__ == "__main__":
    main()
