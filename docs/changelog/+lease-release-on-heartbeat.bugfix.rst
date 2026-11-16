Dropping or releasing a :class:`~filelock.SoftFileLease` on its own heartbeat thread no longer deadlocks that thread, which left every later ``os.fork()`` in the process waiting forever.
