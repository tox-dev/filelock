A :class:`~filelock.SoftFileLock` acquisition whose first read of its new marker's identity failed reads it again
before rolling back. On Windows the rollback could otherwise not recognize the marker it had created, and the empty
marker it left blocked the path for every later acquisition, since no contender breaks an empty marker.
