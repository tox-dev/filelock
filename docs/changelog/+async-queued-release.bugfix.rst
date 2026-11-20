A canceled :class:`~filelock.AsyncFileLock` release now finishes its queued lock-counter decrement after the active
transition ends. This prevents timed-out tasks from leaving a reentrant lock held after all requests finish.
