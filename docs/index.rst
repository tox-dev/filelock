##########
 filelock
##########

A platform-independent file locking library for Python, providing inter-process synchronization:

.. code-block:: python

    from filelock import FileLock

    lock = FileLock("high_ground.txt.lock")
    with lock:
        with open("high_ground.txt", "a") as f:
            f.write("You were the chosen one.")

.. image:: example.gif
    :alt: filelock in action

**************
 Installation
**************

``filelock`` is available via PyPI:

.. code-block:: bash

    python -m pip install filelock

****************
 Learn filelock
****************

.. grid:: 1 2 2 2
    :gutter: 2

    .. grid-item-card::
        **New to file locking?**

        Start with the :doc:`tutorials` to learn the basics through hands-on examples.

    .. grid-item-card::
        **Have a specific task?**

        Check :doc:`how-to` for task-oriented solutions to real-world problems.

    .. grid-item-card::
        **Want to understand the design?**

        Read :doc:`concepts` to explore design decisions and trade-offs.

    .. grid-item-card::
        **Need API details?**

        See the :doc:`api` reference for complete technical documentation.

************
 Lock Types
************

Choose the right lock for your use case:

.. grid:: 1 2 2 2
    :gutter: 2

    .. grid-item-card::
        **FileLock**

        Platform-aware alias. Uses OS-level locking (``fcntl``/``msvcrt``) with automatic fallback to soft locks.

        - ✓ Recommended default
        - ✓ Lifetime expiration, cancellable acquire
        - ✓ Self-deadlock detection

    .. grid-item-card::
        **SoftFileLock**

        File-existence based locking. Works on any filesystem including network mounts.

        - ✓ Network filesystems
        - ✓ Stale detection (Unix)
        - ✓ Lifetime expiration, cancellable acquire

    .. grid-item-card::
        **ReadWriteLock**

        SQLite-backed multiple readers + one writer. Singleton by default.

        - ✓ Concurrent readers
        - ✓ Reentrant per mode
        - ✗ No async, no lifetime

    .. grid-item-card::
        **AsyncFileLock**

        Async-compatible variants. Run blocking I/O in thread pool or custom executor.

        - ✓ Async/await support
        - ✓ All lock types
        - ✓ Custom executor and event loop

******************
 Platform Support
******************

.. grid:: 1 2 2 2
    :gutter: 2

    .. grid-item-card::
        **Windows**

        Uses ``msvcrt.locking``. Enforced by the kernel.

        - ✓ Native support
        - ✓ Most reliable

    .. grid-item-card::
        **Unix / macOS**

        Uses ``fcntl.flock``. POSIX standard, kernel-enforced.

        - ✓ Native support
        - ✓ Stale detection

    .. grid-item-card::
        **Other Platforms**

        Automatic fallback to ``SoftFileLock``. Portable across all filesystems.

        - ✓ Full compatibility
        - ✓ Network filesystems

*******************
 Similar libraries
*******************

- `pid <https://pypi.org/project/pid/>`_ - process ID file locks
- `msvcrt <https://docs.python.org/3/library/msvcrt.html#msvcrt.locking>`_ - Windows file locking (stdlib)
- `fcntl <https://docs.python.org/3/library/fcntl.html#fcntl.flock>`_ - Unix file locking (stdlib)
- `flufl.lock <https://pypi.org/project/flufl.lock/>`_ - Another file locking library
- `fasteners <https://pypi.org/project/fasteners/>`_ - Cross-platform locks and synchronization

.. toctree::
    :hidden:

    self
    tutorials
    how-to
    concepts
    api
    license
    changelog
