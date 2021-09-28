from __future__ import unicode_literals

import sys
import threading

import pytest

from filelock import FileLock, SoftFileLock, Timeout


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_simple(lock_type, tmp_path):
    lock_path = tmp_path / "a"
    lock = lock_type(str(lock_path))

    with lock as locked:
        assert lock.is_locked
        assert lock is locked
    assert not lock.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_nested_context_manager(lock_type, tmp_path):
    # lock is not released before the most outer with statement that locked the lock, is left
    lock_path = tmp_path / "a"
    lock = lock_type(str(lock_path))

    with lock as lock_1:
        assert lock.is_locked
        assert lock is lock_1

        with lock as lock_2:
            assert lock.is_locked
            assert lock is lock_2

            with lock as lock_3:
                assert lock.is_locked
                assert lock is lock_3

            assert lock.is_locked
        assert lock.is_locked
    assert not lock.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_nested_acquire(lock_type, tmp_path):
    # lock is not released before the most outer with statement that locked the lock, is left
    lock_path = tmp_path / "a"
    lock = lock_type(str(lock_path))

    with lock.acquire() as lock_1:
        assert lock.is_locked
        assert lock is lock_1

        with lock.acquire() as lock_2:
            assert lock.is_locked
            assert lock is lock_2

            with lock.acquire() as lock_3:
                assert lock.is_locked
                assert lock is lock_3

            assert lock.is_locked
        assert lock.is_locked
    assert not lock.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_nested_forced_release(lock_type, tmp_path):
    # acquires the lock using a with-statement and releases the lock before leaving the with-statement
    lock_path = tmp_path / "a"
    lock = lock_type(str(lock_path))

    with lock:
        assert lock.is_locked

        lock.acquire()
        assert lock.is_locked

        lock.release(force=True)
        assert not lock.is_locked
    assert not lock.is_locked


class ExThread(threading.Thread):
    def __init__(self, target):
        super(ExThread, self).__init__(target=target)
        self.ex = None

    def run(self):
        try:
            super(ExThread, self).run()
        except Exception:
            self.ex = sys.exc_info()

    def join(self, timeout=None):
        super(ExThread, self).join()
        if self.ex is not None:
            if sys.version_info[0] == 2:
                wrapper_ex = self.ex[1]
                raise (wrapper_ex.__class__, wrapper_ex, self.ex[2])
            raise self.ex[0].with_traceback(self.ex[1], self.ex[2])


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_threaded_shared_lock_obj(lock_type, tmp_path):
    # Runs 100 threads, which need the filelock. The lock must be acquired if at least one thread required it and
    # released, as soon as all threads stopped.
    lock_path = tmp_path / "a"
    lock = lock_type(str(lock_path))

    def thread_work():
        for _ in range(100):
            with lock:
                assert lock.is_locked

    threads = [ExThread(target=thread_work) for i in range(100)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not lock.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_threaded_lock_different_lock_obj(lock_type, tmp_path):
    # Runs multiple threads, which acquire the same lock file with a different FileLock object. When thread group 1
    # acquired the lock, thread group 2 must not hold their lock.

    def thread_work_one():
        for i in range(1000):
            with lock_1:
                assert lock_1.is_locked
                assert not lock_2.is_locked

    def thread_work_two():
        for i in range(1000):
            with lock_2:
                assert not lock_1.is_locked
                assert lock_2.is_locked

    lock_path = tmp_path / "a"
    lock_1, lock_2 = lock_type(str(lock_path)), lock_type(str(lock_path))
    threads = [(ExThread(target=thread_work_one), ExThread(target=thread_work_two)) for i in range(10)]

    for thread_1, thread_2 in threads:
        thread_1.start()
        thread_2.start()
    for thread_1, thread_2 in threads:
        thread_1.join()
        thread_2.join()

    assert not lock_1.is_locked
    assert not lock_2.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_timeout(lock_type, tmp_path):
    # raises Timeout error when the lock cannot be acquired
    lock_path = tmp_path / "a"
    lock_1, lock_2 = lock_type(str(lock_path)), lock_type(str(lock_path))

    # acquire lock 1
    lock_1.acquire()
    assert lock_1.is_locked
    assert not lock_2.is_locked

    # try to acquire lock 2
    with pytest.raises(Timeout, match="The file lock '.*' could not be acquired."):
        lock_2.acquire(timeout=0.1)
    assert not lock_2.is_locked
    assert lock_1.is_locked

    # release lock 1
    lock_1.release()
    assert not lock_1.is_locked
    assert not lock_2.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_default_timeout(lock_type, tmp_path):
    # test if the default timeout parameter works
    lock_path = tmp_path / "a"
    lock1, lock2 = lock_type(str(lock_path)), lock_type(str(lock_path), timeout=0.1)
    assert lock2.timeout == 0.1

    # acquire lock 1
    lock1.acquire()
    assert lock1.is_locked
    assert not lock2.is_locked

    # try to acquire lock 2
    with pytest.raises(Timeout, match="The file lock '.*' could not be acquired."):
        lock2.acquire()
    assert not lock2.is_locked
    assert lock1.is_locked

    lock2.timeout = 0
    assert lock2.timeout == 0

    with pytest.raises(Timeout, match="The file lock '.*' could not be acquired."):
        lock2.acquire()
    assert not lock2.is_locked
    assert lock1.is_locked

    # release lock 1
    lock1.release()
    assert not lock1.is_locked
    assert not lock2.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_context_release_on_exc(lock_type, tmp_path):
    # lock is released when an exception is thrown in a with-statement
    lock_path = tmp_path / "a"
    lock = lock_type(str(lock_path))

    try:
        with lock as lock1:
            assert lock is lock1
            assert lock.is_locked
            raise Exception
    except Exception:
        assert not lock.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_acquire_release_on_exc(lock_type, tmp_path):
    # lock is released when an exception is thrown in a acquire statement
    lock_path = tmp_path / "a"
    lock = lock_type(str(lock_path))

    try:
        with lock.acquire() as lock1:
            assert lock is lock1
            assert lock.is_locked
            raise Exception
    except Exception:
        assert not lock.is_locked


@pytest.mark.skipif(hasattr(sys, "pypy_version_info"), reason="del() does not trigger GC in PyPy")
@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_del(lock_type, tmp_path):
    # lock is released when the object is deleted
    lock_path = tmp_path / "a"
    lock_1, lock_2 = lock_type(str(lock_path)), lock_type(str(lock_path))

    # acquire lock 1
    lock_1.acquire()
    assert lock_1.is_locked
    assert not lock_2.is_locked

    # try to acquire lock 2
    with pytest.raises(Timeout, match="The file lock '.*' could not be acquired."):
        lock_2.acquire(timeout=0.1)

    # delete lock 1 and try to acquire lock 2 again
    del lock_1

    lock_2.acquire()
    assert lock_2.is_locked

    lock_2.release()


def test_cleanup_soft_lock(tmp_path):
    # tests if the lock file is removed after use
    lock_path = tmp_path / "a"
    lock = SoftFileLock(str(lock_path))

    with lock:
        assert lock_path.exists()
    assert not lock_path.exists()
