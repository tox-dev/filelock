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


class BaseTest(object):
    """
    Base class for all filelock tests.
    """

    # The filelock type (class), which is tested.
    LOCK_TYPE = None

    def setUp(self):
        """
        Creates a new lock file:

            self.lock

        and asserts, that it is not locked.
        """
        self.lock = self.LOCK_TYPE("test.lock")
        self.assertFalse(self.lock.is_locked)
        return None

    def tearDown(self):
        """
        Asserts that the lock file *self.lock* is not locked.
        """
        self.assertFalse(self.lock.is_locked)
        return None

    def test_simple(self):
        """
        Asserts that the lock is locked in a context statement and that the
        return value of the *__enter__* method is the lock.
        """
        with self.lock as l:
            self.assertTrue(self.lock.is_locked)
            self.assertTrue(self.lock is l)
        self.assertFalse(self.lock.is_locked)
        return None

    def test_nested(self):
        """
        Asserts, that the lock is not released before the most outer with
        statement that locked the lock, is left.
        """
        with self.lock as l1:
            self.assertTrue(self.lock.is_locked)
            self.assertTrue(self.lock is l1)

            with self.lock as l2:
                self.assertTrue(self.lock.is_locked)
                self.assertTrue(self.lock is l2)

                with self.lock as l3:
                    self.assertTrue(self.lock.is_locked)
                    self.assertTrue(self.lock is l3)

                self.assertTrue(self.lock.is_locked)
            self.assertTrue(self.lock.is_locked)
        self.assertFalse(self.lock.is_locked)
        return None

    def test_nested1(self):
        """
        The same as *test_nested*, but this method uses the *acquire()* method
        to create the lock, rather than the implicit *__enter__* method.
        """
        with self.lock.acquire() as l1:
            self.assertTrue(self.lock.is_locked)
            self.assertTrue(self.lock is l1)

            with self.lock.acquire() as l2:
                self.assertTrue(self.lock.is_locked)
                self.assertTrue(self.lock is l2)

                with self.lock.acquire() as l3:
                    self.assertTrue(self.lock.is_locked)
                    self.assertTrue(self.lock is l3)

                self.assertTrue(self.lock.is_locked)
            self.assertTrue(self.lock.is_locked)
        self.assertFalse(self.lock.is_locked)
        return None

    def test_nested_forced_release(self):
        """
        Acquires the lock using a with-statement and releases the lock
        before leaving the with-statement.
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
        Runs 250 threads, which need the filelock. The lock must be acquired
        if at least one thread required it and released, as soon as all threads
        stopped.
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

        lock1 = self.LOCK_TYPE(self.lock.lock_file)
        lock2 = self.LOCK_TYPE(self.lock.lock_file)

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
        Tests if the lock raises a TimeOut error, when it can not be acquired.
        """
        lock1 = self.LOCK_TYPE(self.lock.lock_file)
        lock2 = self.LOCK_TYPE(self.lock.lock_file)

        # Acquire lock 1.
        lock1.acquire()
        self.assertTrue(lock1.is_locked)
        self.assertFalse(lock2.is_locked)

        # Try to acquire lock 2.
        self.assertRaises(filelock.Timeout, lock2.acquire, timeout=1)
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
        lock1 = self.LOCK_TYPE(self.lock.lock_file)
        lock2 = self.LOCK_TYPE(self.lock.lock_file, timeout = 1)

        self.assertEqual(lock2.timeout, 1)

        # Acquire lock 1.
        lock1.acquire()
        self.assertTrue(lock1.is_locked)
        self.assertFalse(lock2.is_locked)

        # Try to acquire lock 2.
        self.assertRaises(filelock.Timeout, lock2.acquire)
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
        The same as *test_context1()*, but uses the *acquire()* method.
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
        Tests, if the lock is released, when the object is deleted.
        """
        lock1 = self.LOCK_TYPE(self.lock.lock_file)
        lock2 = self.LOCK_TYPE(self.lock.lock_file)

        # Acquire lock 1.
        lock1.acquire()
        self.assertTrue(lock1.is_locked)
        self.assertFalse(lock2.is_locked)

        # Try to acquire lock 2.
        self.assertRaises(filelock.Timeout, lock2.acquire, timeout = 1)

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


class SoftFileLockTest(BaseTest, unittest.TestCase):
    """
    Tests the soft file lock, which is always available.
    """

    LOCK_TYPE = filelock.SoftFileLock


if __name__ == "__main__":
    unittest.main()
