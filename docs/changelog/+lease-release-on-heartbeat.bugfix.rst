A :class:`~filelock.SoftFileLease` garbage collected on its own heartbeat thread no longer deadlocks that thread, which
left every later ``os.fork()`` in the process waiting forever.
