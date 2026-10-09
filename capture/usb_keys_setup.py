#!/usr/bin/env python3
"""Give the USB-keys board (firmware/usb_keys) the relay token over USB serial, on the Pi.
Run with the board plugged into the Pi (~/esp/venv has pyserial, from esptool):

    ~/esp/venv/bin/python ~/dashboard/capture/usb_keys_setup.py [serial-port]
"""
import glob
import os
import sys
import time

import serial

port = sys.argv[1] if len(sys.argv) > 1 else (glob.glob("/dev/serial/by-id/*TVIP*") + glob.glob("/dev/serial/by-id/*ESP32S3*"))[0]
token = open(os.path.expanduser("~/.config/dashboard/relay-token")).read().strip()
with serial.Serial(port, 115200, timeout=2) as s:
    time.sleep(0.5)
    s.reset_input_buffer()
    s.write(f"token {token}\n".encode())
    print(s.readline().decode().strip())     # OK token
    s.write(b"status\n")
    print(s.readline().decode().strip())
