Give each default ``AsyncSoftReadWriteLock`` its own worker so waits on shared locks cannot delay releases of unrelated locks.
