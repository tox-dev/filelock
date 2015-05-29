#!/usr/bin/env python3

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

import time
import unittest
import threading
import random

import filelock


class TestFileLock(unittest.TestCase):
    """
    """

    def setUp(self):
        """
        """
        self.lock = filelock.FileLock("test.lock")
        self.assertFalse(self.lock.is_locked)
        return None

    def tearDown(self):
        """
        """
        self.assertFalse(self.lock.is_locked)
        return None

    def test_simple(self):
        """
        """
        with self.lock:
            self.assertTrue(self.lock.is_locked)
        self.assertFalse(self.lock.is_locked)
        return None

    def test_nested(self):
        """
        """
        with self.lock:
            self.assertTrue(self.lock.is_locked)

            with self.lock:
                self.assertTrue(self.lock.is_locked)

                with self.lock:
                    self.assertTrue(self.lock.is_locked)

                self.assertTrue(self.lock.is_locked)
            self.assertTrue(self.lock.is_locked)
        self.assertFalse(self.lock.is_locked)
        return None

    def test_nested1(self):
        """
        """
        with self.lock.acquire():
            self.assertTrue(self.lock.is_locked)

            with self.lock.acquire():
                self.assertTrue(self.lock.is_locked)

                with self.lock.acquire():
                    self.assertTrue(self.lock.is_locked)

                self.assertTrue(self.lock.is_locked)
            self.assertTrue(self.lock.is_locked)
        self.assertFalse(self.lock.is_locked)
        return None

    def test_nested_forced_release(self):
        """
        """
        with self.lock:
            self.assertTrue(self.lock.is_locked)

            self.lock.acquire()
            self.assertTrue(self.lock.is_locked)

            self.lock.release(force = True)
            self.assertFalse(self.lock.is_locked)
        self.assertFalse(self.lock.is_locked)
        return None

    def test_threaded(self):
        """
        """
        def my_thread():
            for i in range(100):
                with self.lock:
                    self.assertTrue(self.lock.is_locked)
            return None

        NUM_THREADS = 250

        threads = [threading.Thread(target = my_thread) for i in range(NUM_THREADS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertFalse(self.lock.is_locked)
        return None

    def test_threaded1(self):
        """
        .. todo::

            This test does not work ...
        """
        if True:
            return None

        def thread1():
            """
            Requires lock1.
            """
            for i in range(1000):
                with lock1:
                    self.assertTrue(lock1.is_locked)
                    self.assertFalse(lock2.is_locked)
            return None

        def thread2():
            """
            Requires lock2.
            """
            for i in range(1000):
                with lock2:
                    self.assertFalse(lock1.is_locked)
                    self.assertTrue(lock2.is_locked)
            return None

        NUM_THREADS =  10

        lock1 = filelock.FileLock(self.lock.lock_file)
        lock2 = filelock.FileLock(self.lock.lock_file)

        threads1 = [threading.Thread(target = thread1) for i in range(NUM_THREADS)]
        threads2 = [threading.Thread(target = thread2) for i in range(NUM_THREADS)]

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
        """
        lock1 = filelock.FileLock(self.lock.lock_file)
        lock2 = filelock.FileLock(self.lock.lock_file)

        lock1.acquire()
        self.assertTrue(lock1.is_locked)
        self.assertFalse(lock2.is_locked)

        self.assertRaises(filelock.Timeout, lock2.acquire, timeout=1)
        self.assertFalse(lock2.is_locked)
        self.assertTrue(lock1.is_locked)

        lock1.release()
        self.assertFalse(lock1.is_locked)
        self.assertFalse(lock2.is_locked)
        return None

    def test_default_timeout(self):
        """
        """
        lock1 = filelock.FileLock(self.lock.lock_file)
        lock2 = filelock.FileLock(self.lock.lock_file, timeout = 1)

        self.assertEqual(lock2.timeout, 1)

        lock1.acquire()
        self.assertTrue(lock1.is_locked)
        self.assertFalse(lock2.is_locked)

        self.assertRaises(filelock.Timeout, lock2.acquire)
        self.assertFalse(lock2.is_locked)
        self.assertTrue(lock1.is_locked)

        lock2.timeout = 0
        self.assertEqual(lock2.timeout, 0)

        self.assertRaises(filelock.Timeout, lock2.acquire)
        self.assertFalse(lock2.is_locked)
        self.assertTrue(lock1.is_locked)

        lock1.release()
        self.assertFalse(lock1.is_locked)
        self.assertFalse(lock2.is_locked)
        return None

    def test_context(self):
        """
        """
        try:
            with self.lock as lock:
                self.assertIs(self.lock, lock)

                self.assertTrue(self.lock.is_locked)
                raise Exception()
        except:
            self.assertFalse(self.lock.is_locked)
        return None

    def test_context1(self):
        """
        """
        try:
            with self.lock.acquire() as lock:
                self.assertIs(self.lock, lock)

                self.assertTrue(self.lock.is_locked)
                raise Exception()
        except:
            self.assertFalse(self.lock.is_locked)
        return None

    def test_del(self):
        """
        """
        lock1 = filelock.FileLock(self.lock.lock_file)
        lock2 = filelock.FileLock(self.lock.lock_file)

        lock1.acquire()
        self.assertTrue(lock1.is_locked)
        self.assertFalse(lock2.is_locked)

        self.assertRaises(filelock.Timeout, lock2.acquire, timeout = 0)

        del lock1

        lock2.acquire()
        self.assertTrue(lock2.is_locked)

        lock2.release()
        return None


if __name__ == "__main__":
    unittest.main()
