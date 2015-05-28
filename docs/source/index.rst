py-filelock
===========

*py-filelock* is a single Python module, which implements a platform independent
file lock. The lock is thread safe and easy to use:

.. code-block:: python

    lock = filelock.FileLock("my_lock_file")
    with lock:
        shutil.copy("...", "...")

The lock implements also a counter, which allows you to acquire the lock
multiple times without blocking:

.. code-block:: python

    lock = filelock.FileLock("my_lock_file")

    def update_files1():
        with lock:
            assert lock.is_locked
            # ...
        return None

    def update_files2():
        with lock:
            assert lock.is_locked
            # ...
        return None

    def update_all_files():
        with lock:
            assert lock.is_locked

            update_files1()

            assert lock.is_locked

            update_files2()

            assert lock.is_locked
        assert not lock.is_locked
        return None

    update_all_files()


Installation
------------

This package is listed on PyPi, so you're done with:

.. code-block:: bash

    $ pip3 install filelock


Examples
--------

.. code-block:: python

    import filelock

    lock = filelock.FileLock("my_lock_file")

    # Simply use the lock into a with statement.
    with lock:
        pass

    # If you want to set a timeout parameter, you can do it by:
    with lock.acquire(timeout = 10):
        pass

    # You can also set a default timeout value, which is used, when no
    # special timeout value is given to the *acquire()* method:
    lock.timeout = 20

    with lock: # 20s timeout
        pass

    with lock.acquire() # 20s timeout
        pass

    with lock.acquire(timeout = 10) # 10s timeout
        pass

    # If you want to use a timeout value, you should consider to catch
    # a Timeout exception:
    try:
        with lock.acquire(timeout = 10):
            pass
    except filelock.Timeout:
        pass

    # If you can not use the *with* statement, use a try-finally construct
    # instead:
    lock.acquire()
    try:
        pass
    finally:
        lock.release()

    # Please note, that you can acquire the lock multiple times without
    # blocking. The lock will count, how often it has been acquired and releases
    # the lock, as soon as the counter is 0.
    with lock:
        assert lock.is_locked
        with lock:
            assert lock.is_locked
        assert lock.is_locked
    assert (not lock.is_locked)


API
---

.. autoclass:: filelock.Timeout
    :show-inheritance:

.. autoclass:: filelock.FileLock
    :members:
    :inherited-members:


License
-------

*py-filelock* is public domain:

.. code-block:: none

    This is free and unencumbered software released into the public domain.

    Anyone is free to copy, modify, publish, use, compile, sell, or
    distribute this software, either in source code form or as a compiled
    binary, for any purpose, commercial or non-commercial, and by any
    means.

    In jurisdictions that recognize copyright laws, the author or authors
    of this software dedicate any and all copyright interest in the
    software to the public domain. We make this dedication for the benefit
    of the public at large and to the detriment of our heirs and
    successors. We intend this dedication to be an overt act of
    relinquishment in perpetuity of all present and future rights to this
    software under copyright law.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
    EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
    MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
    IN NO EVENT SHALL THE AUTHORS BE LIABLE FOR ANY CLAIM, DAMAGES OR
    OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
    ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
    OTHER DEALINGS IN THE SOFTWARE.

    For more information, please refer to <http://unlicense.org>


GitHub
------

This module is hosted on
`GitHub <https://github.com/benediktschmitt/py-filelock>`_. If you have any
questions or suggestions, don't hesitate to open a new issue :).
