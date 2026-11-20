``SoftReadWriteLock`` and ``AsyncSoftReadWriteLock`` bound each generation log scan, so an account that keeps adding
records cannot hold an acquisition inside one scan past its timeout. Release raises a protocol error when it cannot
withdraw the holder before its stale threshold.
