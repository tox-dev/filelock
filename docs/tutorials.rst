###########
 Tutorials
###########

This section guides you through the fundamentals of file locking. We'll learn by doing, starting with the basics and
building up to more advanced patterns.

*****************
 Your first lock
*****************

Let's create our first lock and use it to coordinate between processes.

First, we'll import what we need and create a lock object:

.. code-block:: python

    from pathlib import Path
    from filelock import FileLock

    lock = FileLock("myapp.lock")

Now we have a lock object that represents a lock file on disk. We can use the lock with a context manager (the ``with``
statement):

.. code-block:: python

    with lock:
        # Inside this block, we hold the lock
        print("I have the lock!")
    # Outside this block, the lock is released

Run this code multiple times in different terminal windows at the same time. You'll see that only one process prints the
message at a time—the others wait for their turn. The lock is working correctly!

************************
 Protecting shared data
************************

File locks are most useful when protecting data that multiple processes access. Let's see how:

.. code-block:: python

    from pathlib import Path
    from filelock import FileLock

    data_file = Path("data.txt")
    lock = FileLock("data.txt.lock")

    # Process A writes a greeting
    with lock:
        if not data_file.exists():
            data_file.write_text("Hello from Process A\n")

    # Process B appends another greeting
    with lock:
        with data_file.open("a") as f:
            f.write("Hello from Process B\n")

The key pattern here: **Before making changes, check what's already done.** Process A checks if the file exists before
writing. Process B doesn't need to check because it's just appending. But both use the lock to ensure only one process
modifies the file at a time.

Run this code from two different processes. The file will contain messages from both in a consistent order.

*****************
 Reentrant locks
*****************

Sometimes you need to acquire the same lock multiple times from the same process or thread. The lock allows this:

.. code-block:: python

    from filelock import FileLock

    lock = FileLock("reentrant.lock")


    def helper_function():
        with lock:
            print("Helper has the lock")


    with lock:
        print("Main code has the lock")
        helper_function()  # Can acquire the same lock again
        print("Still have the lock")

No deadlock occurs—the lock counts how many times it's been acquired and releases only when the count reaches zero. You
can inspect this counter and the lock state at any time:

.. code-block:: python

    lock = FileLock("reentrant.lock")

    print(lock.is_locked)     # False
    print(lock.lock_counter)  # 0

    lock.acquire()
    print(lock.is_locked)     # True
    print(lock.lock_counter)  # 1

    lock.acquire()
    print(lock.lock_counter)  # 2

    lock.release()
    print(lock.lock_counter)  # 1 — still locked
    print(lock.is_locked)     # True

    lock.release()
    print(lock.lock_counter)  # 0 — fully released
    print(lock.is_locked)     # False

Key lesson: You can safely call functions that acquire a lock, even if you already hold the lock.

*****************************
 Multiple ways to use a lock
*****************************

So far we've used the ``with`` statement. There are other ways:

**Manual acquire and release:**

.. code-block:: python

    lock.acquire()
    try:
        print("I have the lock")
    finally:
        lock.release()

Always use a ``try/finally`` block to guarantee the lock is released, even if an exception occurs.

**As a decorator:**

.. code-block:: python

    @lock
    def protected_operation():
        print("This function runs with the lock held")


    protected_operation()  # Lock is acquired, function runs, lock is released

Choose whichever feels most natural for your code. The ``with`` statement is usually clearest.

Important: Always use the context manager
=========================================

Avoid this pattern:

.. code-block:: python

    FileLock("my.lock").acquire()  # ⚠️ Don't do this
    # The lock might be released during garbage collection
    # before your code finishes

This doesn't work reliably because if you don't assign the lock to a variable, Python's garbage collector might release
it before you're done with it.

Instead, always keep a reference to the lock object:

.. code-block:: python

    lock = FileLock("my.lock")
    with lock:  # ✓ Good
        # your code here
        pass

**************************
 Thread-local by default
**************************

By default, each lock uses thread-local state (``thread_local=True``). This means each thread tracks its own lock
counter independently. Two threads holding the same ``FileLock`` object each get their own reentrant state.

Async locks default to ``thread_local=False`` because they run in a thread pool where the acquiring thread may differ
from the releasing thread.

See :ref:`how-to:Use locks with multiple threads` for practical examples of controlling this behavior.

************
 Next steps
************

- Want to handle timeouts, cancellation, or force-release? See :doc:`how-to`.
- Curious about how locks work across different platforms? Read :doc:`concepts`.
