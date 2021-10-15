filelock
========

This package contains a single module, which implements a platform independent file lock in Python, which provides a
simple way of inter-process communication:

.. code-block:: python

    from filelock import Timeout, FileLock

    lock = FileLock("high_ground.txt.lock")
    with lock:
        open("high_ground.txt", "a").write("You were the chosen one.")

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
- for UNIX the `fcntl <https://docs.python.org/3/library/fcntl.html#fcntl.flock>`_ module in the standard library.

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

    from filelock import Timeout, FileLock

    file_path = "high_ground.txt"
    lock_path = "high_ground.txt.lock"

    lock = FileLock(lock_path, timeout=1)

The lock object supports multiple ways for acquiring the lock, including the ones used to acquire standard Python thread
locks:

.. code-block:: python

    with lock:
        open(file_path, "a").write("Hello there!")

    lock.acquire()
    try:
        open(file_path, "a").write("General Kenobi!")
    finally:
        lock.release()

The :meth:`acquire <filelock.BaseFileLock.acquire>` method accepts also a ``timeout`` parameter. If the lock cannot be
acquired within ``timeout`` seconds, a :class:`Timeout <filelock.Timeout>` exception is raised:

.. code-block:: python

    try:
        with lock.acquire(timeout=10):
            open(file_path, "a").write("I have a bad feeling about this.")
    except Timeout:
        print("Another instance of this application currently holds the lock.")

The lock objects are recursive locks, which means that once acquired, they will not block on successive lock requests:

.. code-block:: python

    def cite1():
        with lock:
            open(file_path, "a").write("I hate it when he does that.")


    def cite2():
        with lock:
            open(file_path, "a").write("You don't want to sell me death sticks.")


    # The lock is acquired here.
    with lock:
        cite1()
        cite2()
    # And released here.


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
is not. Use the :class:`FileLock <filelock.FileLock>` if all instances of your application are running on the same host
and a :class:`SoftFileLock <filelock.SoftFileLock>` otherwise.

The :class:`SoftFileLock <filelock.SoftFileLock>` only watches the existence of the lock file. This makes it ultra
portable, but also more prone to dead locks if the application crashes. You can simply delete the lock file in such
cases.

Asyncio support
---------------

This library currently does not support asyncio. We'd recommend adding an asyncio variant though if someone can make a
pull request for it, `see here <https://github.com/tox-dev/py-filelock/issues/99>`_.

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
