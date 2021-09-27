#!/usr/bin/env python3

import sys
from time import sleep

from src.filelock import FileLock

lock = FileLock("my_lock")
with lock:
    print("This is process {}.".format(sys.argv[1]))
    sleep(1)
    print("Bye.")
