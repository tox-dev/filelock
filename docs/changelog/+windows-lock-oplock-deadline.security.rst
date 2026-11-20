Windows ``FileLock`` treats an unanswered lock-file oplock as contention within the current acquire attempt, so short
timeouts and ``blocking=False`` do not wait for the opener's grace period.
