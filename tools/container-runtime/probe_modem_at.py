#!/usr/bin/env python3
"""Read non-identifying modem status over an AT serial port.

This deliberately avoids IMSI, ICCID, phone-number and cell-location commands.
It does not change modem configuration.
"""
import argparse
import os
import select
import termios
import time


def configure(fd):
    attrs = termios.tcgetattr(fd)
    attrs[0] = 0
    attrs[1] = 0
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    attrs[3] = 0
    attrs[4] = termios.B115200
    attrs[5] = termios.B115200
    attrs[6][termios.VMIN] = 0
    attrs[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    termios.tcflush(fd, termios.TCIOFLUSH)


def query(fd, command, timeout=3.0):
    os.write(fd, command.encode("ascii") + b"\r")
    chunks = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        readable, _, _ = select.select([fd], [], [], min(0.2, deadline - time.monotonic()))
        if not readable:
            continue
        chunk = os.read(fd, 4096)
        if chunk:
            chunks.append(chunk)
            value = b"".join(chunks)
            if b"\r\nOK\r\n" in value or b"\r\nERROR\r\n" in value \
                    or b"+CME ERROR:" in value:
                return value.decode("ascii", "replace").strip()
    raise TimeoutError(f"{command} did not answer within {timeout:g}s")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("device", nargs="?", default="/dev/ttyUSB2")
    args = parser.parse_args()
    fd = os.open(args.device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        configure(fd)
        for command in ("AT", "ATI", "AT+CPIN?", "AT+CSQ", "AT+CFUN?"):
            print(f"[{command}]\n{query(fd, command)}")
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
