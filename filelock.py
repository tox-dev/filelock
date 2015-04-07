#!/usr/bin/python3

# This is free and unencumbered software released into the public domain.
#
# Anyone is free to copy, modify, publish, use, compile, sell, or
# distribute this software, either in source code form or as a compiled
# binary, for any purpose, commercial or non-commercial, and by any
# means.
#
# In jurisdictions that recognize copyright laws, the author or authors
# of this software dedicate any and all copyright interest in the
# software to the public domain. We make this dedication for the benefit
# of the public at large and to the detriment of our heirs and
# successors. We intend this dedication to be an overt act of
# relinquishment in perpetuity of all present and future rights to this
# software under copyright law.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# IN NO EVENT SHALL THE AUTHORS BE LIABLE FOR ANY CLAIM, DAMAGES OR
# OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
# ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.
#
# For more information, please refer to <http://unlicense.org>

"""
A platform independent file lock that supports the with-statement.
"""


# Modules
# ------------------------------------------------
import time
import atexit
import os
import threading
try:
    import warnings
except ImportError:
    warnings = None

try:
    import msvcrt
except ImportError:
    msvcrt = None

try:
    import fcntl
except ImportError:
    fcntl = None


# Backward compatibility
# ------------------------------------------------
try:
    TimeoutError
except NameError:
    TimeoutError = OSError


# Data
# ------------------------------------------------
__all__ = ["Timeout", "FileLock"]
__version__ = "1.0.0"

# Exceptions
# ------------------------------------------------
class Timeout(TimeoutError):
    """
    Raised when the lock could not be acquired in *timeout*
    seconds.
    """

    def __init__(self, lock_file):
        self.lock_file = lock_file
        return None

    def __str__(self):
        temp = "The file lock '{}' could not be acquired."\
               .format(self.lock_file)
        return temp


# Classes
# ------------------------------------------------
class BaseFileLock(object):
    """
    Implements the base class of a file lock.

    The file lock counts how often your acquired the filelock and will
    release it only if *release* has been called as often as *acquire*.

    Usage:

    .. code-block:: python

        with BaseFileLock("afile"):
            pass

    or if you need to specify a timeout:

    .. code-block:: python

        with BaseFileLock("afile").acquire(5):
            pass

    The lock counter works like this:

    .. code-block:: python

        lock = BaseFileLock("afile")
        with lock:
            with lock:
                pass
            assert lock.is_locked()
    """

    def __init__(self, lock_file):
        self._lock_file = lock_file
        self._lock_file_fd = None

        # We use this lock primarily for the lock counter.
        self._thread_lock = threading.Lock()

        self._lock_counter = 0

        atexit.register(self.release)
        return None

    lock_file = property(lambda self: self._lock_file)

    # Platform dependent locking
    # --------------------------------------------

    def _acquire(self):
        """
        Platform dependent. If the file lock could be
        acquired, self._lock_file_fd holds the file descriptor
        of the lock file.
        """
        raise NotImplementedError()

    def _release(self):
        """
        Releases the lock and sets self._lock_file_fd to None.
        """
        raise NotImplementedError()

    # Platform independent methods
    # --------------------------------------------

    def is_locked(self):
        """
        Returns true, if the object holds the file lock.
        """
        return self._lock_file_fd is not None

    def acquire(self, timeout=None, poll_intervall=0.05):
        """
        Tries every *poll_intervall* seconds to acquire the lock.
        If the lock could not be acquired after *timeout* seconds,
        a Timeout exception will be raised.
        If *timeout* is ``None``, there's no time limit.
        """
        # Increment the number right at the beginning.
        # We can still undo it, if something fails.
        with self._thread_lock:
            self._lock_counter += 1

        try:
            start_time = time.time()
            while not self.is_locked():
                self._acquire()

                if timeout is not None and time.time() - start_time > timeout:
                    raise Timeout(self._lock_file)

                time.sleep(poll_intervall)
        except:
            # Something did go wrong, so decrement the counter.
            with self._thread_lock:
                self._lock_counter = max(0, self._lock_counter - 1)

            raise
        return self

    def release(self, force = False):
        """
        Releases the file lock.

        :arg bool force:
            If true, the lock counter is ignored and the lock is released in
            every case.
        """
        with self._thread_lock:

            if self.is_locked():
                self._lock_counter -= 1

                if self._lock_counter == 0 or force:
                    self._release()
                    self._lock_counter = 0
        return None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()
        return None

    def __del__(self):
        self.release()
        return None


# Windows locking mechanism
if msvcrt:
    class FileLock(BaseFileLock):

        def _acquire(self):
            open_mode = os.O_RDWR | os.O_CREAT | os.O_TRUNC
            fd = os.open(self._lock_file, open_mode)

            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                os.close(fd)
            else:
                self._lock_file_fd = fd
            return None

        def _release(self):
            msvcrt.locking(self._lock_file_fd, msvcrt.LK_UNLCK, 1)
            os.close(self._lock_file_fd)
            self._lock_file_fd = None

            try:
                os.remove(self._lock_file)
            # Probably another instance of the application
            # that acquired the file lock.
            except OSError:
                pass
            return None

# Unix locking mechanism
elif fcntl:
    class FileLock(BaseFileLock):

        def _acquire(self):
            open_mode = os.O_RDWR | os.O_CREAT | os.O_TRUNC
            fd = os.open(self._lock_file, open_mode)

            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (IOError, OSError):
                os.close(fd)
            else:
                self._lock_file_fd = fd
            return None

        def _release(self):
            fcntl.flock(self._lock_file_fd, fcntl.LOCK_UN)
            os.close(self._lock_file_fd)
            self._lock_file_fd = None

            try:
                os.remove(self._lock_file)
            # Probably another instance of the application
            # that acquired the file lock.
            except OSError:
                pass
            return None

# The "hard" lock is not available. But we can watch the existence of a file.
else:
    class FileLock(BaseFileLock):

        def _acquire(self):
            open_mode = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_TRUNC
            try:
                fd = os.open(self._lock_file, open_mode)
            except (IOError, OSError):
                pass
            else:
                self._lock_file_fd = fd
            return None

        def _release(self):
            os.close(self._lock_file_fd)
            self._lock_file_fd = None

            try:
                os.remove(self._lock_file)
            # The file is already deleted and that's what we want.
            except OSError:
                pass
            return None

    if warnings is not None:
        warnings.warn("only soft file lock is available")
