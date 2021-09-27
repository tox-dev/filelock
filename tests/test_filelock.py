#!/usr/bin/env python

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
Some tests for the file lock.
"""

import errno
import os
import sys
import threading
import unittest

import filelock

PY2 = sys.version_info[0] == 2
PY3 = sys.version_info[0] == 3


class ExThread(threading.Thread):
    def __init__(self, *args, **kargs):
        threading.Thread.__init__(self, *args, **kargs)
        self.ex = None
        return None

    def run(self):
        try:
            threading.Thread.run(self)
        except:
            self.ex = sys.exc_info()
        return None

    def join(self):
        threading.Thread.join(self)
        if self.ex is not None:
            if PY3:
                raise self.ex[0].with_traceback(self.ex[1], self.ex[2])
            elif PY2:
                wrapper_ex = self.ex[1]
                raise (wrapper_ex.__class__, wrapper_ex, self.ex[2])
        return None


class BaseTest(object):
    """
    Base class for all filelock tests.
    """

    # The filelock type (class), which is tested.
    LOCK_TYPE = None

    # The path to the lockfile.
    LOCK_PATH = "test.lock"

    def setUp(self):
        """Deletes the potential lock file at :attr:`LOCK_PATH`."""
        try:
            os.remove(self.LOCK_PATH)
        except OSError as e:
            # FileNotFound
            if e.errno != errno.ENOENT:
                raise
        return None

    def tearDown(self):
        """Deletes the potential lock file at :attr:`LOCK_PATH`."""
        try:
            os.remove(self.LOCK_PATH)
        except OSError as e:
            # FileNotFound
            if e.errno != errno.ENOENT:
                raise
        return None

    def test_simple(self):
        """
        Asserts that the lock is locked in a context statement and that the
        return value of the *__enter__* method is the lock.
        """
        lock = self.LOCK_TYPE(self.LOCK_PATH)

        with lock as l:
            self.assertTrue(lock.is_locked)
            self.assertTrue(lock is l)
        self.assertFalse(lock.is_locked)
        return None

    def test_nested(self):
        """
        Asserts, that the lock is not released before the most outer with
        statement that locked the lock, is left.
        """
        lock = self.LOCK_TYPE(self.LOCK_PATH)

        with lock as l1:
            self.assertTrue(lock.is_locked)
            self.assertTrue(lock is l1)

            with lock as l2:
                self.assertTrue(lock.is_locked)
                self.assertTrue(lock is l2)

                with lock as l3:
                    self.assertTrue(lock.is_locked)
                    self.assertTrue(lock is l3)

                self.assertTrue(lock.is_locked)
            self.assertTrue(lock.is_locked)
        self.assertFalse(lock.is_locked)
        return None

    def test_nested1(self):
        """
        The same as *test_nested*, but this method uses the *acquire()* method
        to create the lock, rather than the implicit *__enter__* method.
        """
        lock = self.LOCK_TYPE(self.LOCK_PATH)

        with lock.acquire() as l1:
            self.assertTrue(lock.is_locked)
            self.assertTrue(lock is l1)

            with lock.acquire() as l2:
                self.assertTrue(lock.is_locked)
                self.assertTrue(lock is l2)

                with lock.acquire() as l3:
                    self.assertTrue(lock.is_locked)
                    self.assertTrue(lock is l3)

                self.assertTrue(lock.is_locked)
            self.assertTrue(lock.is_locked)
        self.assertFalse(lock.is_locked)
        return None

    def test_nested_forced_release(self):
        """
        Acquires the lock using a with-statement and releases the lock
        before leaving the with-statement.
        """
        lock = self.LOCK_TYPE(self.LOCK_PATH)

        with lock:
            self.assertTrue(lock.is_locked)

            lock.acquire()
            self.assertTrue(lock.is_locked)

            lock.release(force=True)
            self.assertFalse(lock.is_locked)
        self.assertFalse(lock.is_locked)
        return None

    def test_threaded(self):
        """
        Runs 250 threads, which need the filelock. The lock must be acquired
        if at least one thread required it and released, as soon as all threads
        stopped.
        """
        lock = self.LOCK_TYPE(self.LOCK_PATH)

        def my_thread():
            for i in range(100):
                with lock:
                    self.assertTrue(lock.is_locked)
            return None

        NUM_THREADS = 250

        threads = [ExThread(target=my_thread) for i in range(NUM_THREADS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertFalse(lock.is_locked)
        return None

    def test_threaded1(self):
        """
        Runs multiple threads, which acquire the same lock file with a different
        FileLock object. When thread group 1 acquired the lock, thread group 2
        must not hold their lock.
        """

        def thread1():
            """
            Requires lock1.
            """
            for i in range(1000):
                with lock1:
                    self.assertTrue(lock1.is_locked)
                    self.assertFalse(lock2.is_locked)  # FIXME (Filelock)
            return None

        def thread2():
            """
            Requires lock2.
            """
            for i in range(1000):
                with lock2:
                    self.assertFalse(lock1.is_locked)  # FIXME (FileLock)
                    self.assertTrue(lock2.is_locked)
            return None

        NUM_THREADS = 10

        lock1 = self.LOCK_TYPE(self.LOCK_PATH)
        lock2 = self.LOCK_TYPE(self.LOCK_PATH)

        threads1 = [ExThread(target=thread1) for i in range(NUM_THREADS)]
        threads2 = [ExThread(target=thread2) for i in range(NUM_THREADS)]

        for i in range(NUM_THREADS):
            threads1[i].start()
            threads2[i].start()
        for i in range(NUM_THREADS):
            threads1[i].join()
            threads2[i].join()

        self.assertFalse(lock1.is_locked)
        self.assertFalse(lock2.is_locked)
        return None

    def test_timeout(self):
        """
        Tests if the lock raises a TimeOut error, when it can not be acquired.
        """
        lock1 = self.LOCK_TYPE(self.LOCK_PATH)
        lock2 = self.LOCK_TYPE(self.LOCK_PATH)

        # Acquire lock 1.
        lock1.acquire()
        self.assertTrue(lock1.is_locked)
        self.assertFalse(lock2.is_locked)

        # Try to acquire lock 2.
        self.assertRaises(filelock.Timeout, lock2.acquire, timeout=1)  # FIXME (Filelock)
        self.assertFalse(lock2.is_locked)
        self.assertTrue(lock1.is_locked)

        # Release lock 1.
        lock1.release()
        self.assertFalse(lock1.is_locked)
        self.assertFalse(lock2.is_locked)
        return None

    def test_default_timeout(self):
        """
        Test if the default timeout parameter works.
        """
        lock1 = self.LOCK_TYPE(self.LOCK_PATH)
        lock2 = self.LOCK_TYPE(self.LOCK_PATH, timeout=1)

        self.assertEqual(lock2.timeout, 1)

        # Acquire lock 1.
        lock1.acquire()
        self.assertTrue(lock1.is_locked)
        self.assertFalse(lock2.is_locked)

        # Try to acquire lock 2.
        self.assertRaises(filelock.Timeout, lock2.acquire)  # FIXME (SoftFileLock)
        self.assertFalse(lock2.is_locked)
        self.assertTrue(lock1.is_locked)

        lock2.timeout = 0
        self.assertEqual(lock2.timeout, 0)

        self.assertRaises(filelock.Timeout, lock2.acquire)
        self.assertFalse(lock2.is_locked)
        self.assertTrue(lock1.is_locked)

        # Release lock 1.
        lock1.release()
        self.assertFalse(lock1.is_locked)
        self.assertFalse(lock2.is_locked)
        return None

    def test_context(self):
        """
        Tests, if the filelock is released, when an exception is thrown in
        a with-statement.
        """
        lock = self.LOCK_TYPE(self.LOCK_PATH)

        try:
            with lock as lock1:
                self.assertIs(lock, lock1)
                self.assertTrue(lock.is_locked)
                raise Exception()
        except:
            self.assertFalse(lock.is_locked)
        return None

    def test_context1(self):
        """
        The same as *test_context1()*, but uses the *acquire()* method.
        """
        lock = self.LOCK_TYPE(self.LOCK_PATH)

        try:
            with lock.acquire() as lock1:
                self.assertIs(lock, lock1)
                self.assertTrue(lock.is_locked)
                raise Exception()
        except:
            self.assertFalse(lock.is_locked)
        return None

    @unittest.skipIf(hasattr(sys, "pypy_version_info"), "del() does not trigger GC in PyPy")
    def test_del(self):
        """
        Tests, if the lock is released, when the object is deleted.
        """
        lock1 = self.LOCK_TYPE(self.LOCK_PATH)
        lock2 = self.LOCK_TYPE(self.LOCK_PATH)

        # Acquire lock 1.
        lock1.acquire()
        self.assertTrue(lock1.is_locked)
        self.assertFalse(lock2.is_locked)

        # Try to acquire lock 2.
        self.assertRaises(filelock.Timeout, lock2.acquire, timeout=1)  # FIXME (SoftFileLock)

        # Delete lock 1 and try to acquire lock 2 again.
        del lock1

        lock2.acquire()
        self.assertTrue(lock2.is_locked)

        lock2.release()
        return None


class FileLockTest(BaseTest, unittest.TestCase):
    """
    Tests the hard file lock, which is available on the current platform.
    """

    LOCK_TYPE = filelock.FileLock
    LOCK_PATH = "test.lock"


class SoftFileLockTest(BaseTest, unittest.TestCase):
    """
    Tests the soft file lock, which is always available.
    """

    LOCK_TYPE = filelock.SoftFileLock
    LOCK_PATH = "test.softlock"

    def test_cleanup(self):
        """
        Tests if the lock file is removed after use.
        """
        lock = self.LOCK_TYPE(self.LOCK_PATH)

        with lock:
            self.assertTrue(os.path.exists(self.LOCK_PATH))
        self.assertFalse(os.path.exists(self.LOCK_PATH))
        return None


if __name__ == "__main__":
    unittest.main()
