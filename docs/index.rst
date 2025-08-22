filelock
========

This package contains a single module, which implements a platform independent file lock in Python, which provides a
simple way of inter-process communication:

.. code-block:: python

    from filelock import Timeout, FileLock

    lock = FileLock("high_ground.txt.lock")
    with lock:
        with open("high_ground.txt", "a") as f:
            f.write("You were the chosen one.")

**Don't use** a :class:`FileLock <filelock.FileLock>` to lock the file you want to write to, instead create a separate
``.lock`` file as shown above.

.. image:: example.gif
  :alt: Example gif


Similar libraries
-----------------

Perhaps you are looking for something like:

- the `pid <https://pypi.python.org/pypi/pid>`_ 3rd party library,
- for Windows the `msvcrt <https://docs.python.org/3/library/msvcrt.html#msvcrt.locking>`_ module in the standard
  library,
- for UNIX the `fcntl <https://docs.python.org/3/library/fcntl.html#fcntl.flock>`_ module in the standard library,
- the `flufl.lock <https://pypi.org/project/flufl.lock/>`_ 3rd party library.


Installation
------------

``filelock`` is available via PyPI, so you can pip install it:

.. code-block:: bash

    python -m pip install filelock

Tutorial
--------

A :class:`FileLock <filelock.FileLock>` is used to indicate another process of your application that a resource or
working directory is currently used. To do so, create a :class:`FileLock <filelock.FileLock>` first:

.. code-block:: python

    import os
    from filelock import Timeout, FileLock

    file_path = "high_ground.txt"
    lock_path = "high_ground.txt.lock"

    lock = FileLock(lock_path, timeout=1)

The lock object represents an exclusive/write lock and can be acquired in multiple ways, including the ones used to acquire standard Python thread
locks:

.. code-block:: python

    with lock:
        if not os.path.exists(file_path):
            with open(file_path, "w") as f:
                f.write("Hello there!")
    # here, all processes can see consistent content in the file

    lock.acquire()
    try:
        if not os.path.exists(file_path):
            with open(file_path, "w") as f:
                f.write("General Kenobi!")
    finally:
        lock.release()
    # here, all processes can see consistent content in the file

    @lock
    def decorated():
        print("You're a decorated Jedi!")


    decorated()

Note: When a process gets the lock (i.e. within the `with lock:` region), it is usually good to check what has
already been done by other processes. For example, each process above first check the existence of the file. If
it is already created, we should not destroy the work of other processes. This is typically the case when we want
just one process to write content into a file, and let every process to read the content.

The lock objects are recursive locks, which means that once acquired, they will not block on successive lock requests:

.. code-block:: python

    def cite1():
        with lock:
            with open(file_path, "a") as f:
                f.write("I hate it when he does that.")


    def cite2():
        with lock:
            with open(file_path, "a") as f:
                f.write("You don't want to sell me death sticks.")


    # The lock is acquired here.
    with lock:
        cite1()
        cite2()
    # And released here.

It should also be noted that the lock is released during garbage collection.
Put another way, if you do not assign an acquired lock to a variable,
the lock will eventually be released (implicitly, not explicitly).
For this reason, using the lock in the way shown below is not something you should ever do,
always use the context manager (`with` form) instead.

This issue is illustrated with code below:

.. code-block:: python

    import tempfile
    from pathlib import Path

    import filelock

    # If you create a lock and acquire it but don't assign it,
    # you will not actually hold the lock forever.
    # Instead, the lock is released
    # when the created variable is garbage collected.
    FileLock(lock_path).acquire()
    # At some point after the creation above,
    # the lock is released again even though there is no explicit call to `release`.

    # If you instead assign to a dummy variable,
    # the lock will be hold
    _ = FileLock(lock_path).acquire()
    # Now the lock is being held
    # (at least until `_` is reassigned and the lock is garbage collected)

Timeouts and non-blocking locks
-------------------------------
The :meth:`acquire <filelock.BaseFileLock.acquire>` method accepts a ``timeout`` parameter. If the lock cannot be
acquired within ``timeout`` seconds, a :class:`Timeout <filelock.Timeout>` exception is raised:

.. code-block:: python

    try:
        with lock.acquire(timeout=10):
            with open(file_path, "a") as f:
                f.write("I have a bad feeling about this.")
    except Timeout:
        print("Another instance of this application currently holds the lock.")

Using a ``timeout < 0`` makes the lock block until it can be acquired
while ``timeout == 0`` results in only one attempt to acquire the lock before raising a :class:`Timeout <filelock.Timeout>` exception (-> non-blocking).

You can also use the ``blocking`` parameter to attempt a non-blocking :meth:`acquire <filelock.BaseFileLock.acquire>`.

.. code-block:: python

    try:
        with lock.acquire(blocking=False):
            with open(file_path, "a") as f:
                f.write("I have a bad feeling about this.")
    except Timeout:
        print("Another instance of this application currently holds the lock.")


The ``blocking`` option takes precedence over ``timeout``.
Meaning, if you set ``blocking=False`` while ``timeout > 0``, a :class:`Timeout <filelock.Timeout>` exception is raised without waiting for the lock to release.

You can pre-parametrize both of these options when constructing the lock for ease-of-use.

.. code-block:: python

    from filelock import Timeout, FileLock

    lock_1 = FileLock("high_ground.txt.lock", blocking = False)
    try:
        with lock_1:
            # do some work
            pass
    except Timeout:
        print("Well, we tried once and couldn't acquire.")

    lock_2 = FileLock("high_ground.txt.lock", timeout = 10)
    try:
        with lock_2:
            # do some other work
            pass
    except Timeout:
        print("Ten seconds feel like forever sometimes.")

Logging
-------
All log messages by this library are made using the ``DEBUG_ level``, under the ``filelock`` name. On how to control
displaying/hiding that please consult the
`logging documentation of the standard library <https://docs.python.org/3/howto/logging.html>`_. E.g. to hide these
messages you can use:

.. code-block:: python

    logging.getLogger("filelock").setLevel(logging.INFO)

FileLock vs SoftFileLock
------------------------

The :class:`FileLock <filelock.FileLock>` is platform dependent while the :class:`SoftFileLock <filelock.SoftFileLock>`
is not. Use the :class:`FileLock <filelock.FileLock>` if all instances of your application are running on the same
platform and a :class:`SoftFileLock <filelock.SoftFileLock>` otherwise.

The :class:`SoftFileLock <filelock.SoftFileLock>` only watches the existence of the lock file. This makes it ultra
portable, but also more prone to dead locks if the application crashes. You can simply delete the lock file in such
cases.

Asyncio support
---------------

This library currently does not support asyncio. We'd recommend adding an asyncio variant though if someone can make a
pull request for it, `see here <https://github.com/tox-dev/py-filelock/issues/99>`_.

FileLocks and threads
---------------------

By default the :class:`FileLock <filelock.FileLock>` internally uses :class:`threading.local <threading.local>`
to ensure that the lock is thread-local. If you have a use case where you'd like an instance of ``FileLock`` to be shared
across threads, you can set the ``thread_local`` parameter to ``False`` when creating a lock. For example:

.. code-block:: python

    lock = FileLock("test.lock", thread_local=False)
    # lock will be re-entrant across threads

    # The same behavior would also work with other instances of BaseFileLock like SoftFileLock:
    soft_lock = SoftFileLock("soft_test.lock", thread_local=False)
    # soft_lock will be re-entrant across threads.


Behavior where :class:`FileLock <filelock.FileLock>` is thread-local started in version 3.11.0. Previous versions,
were not thread-local by default.

Note: If disabling thread-local, be sure to remember that locks are re-entrant: You will be able to
:meth:`acquire <filelock.BaseFileLock.acquire>` the same lock multiple times across multiple threads.

Contributions and issues
------------------------

Contributions are always welcome, please make sure they pass all tests before creating a pull request. This module is
hosted on `GitHub <https://github.com/tox-dev/py-filelock>`_. If you have any questions or suggestions, don't hesitate
to open a new issue 😊. There's no bad question, just a missed opportunity to learn more.

.. toctree::
   :hidden:

   self
   api
   license
   changelog
