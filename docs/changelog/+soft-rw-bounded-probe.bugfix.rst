``SoftReadWriteLock`` and ``AsyncSoftReadWriteLock`` bound each generation log scan, so an account that keeps adding
records cannot hold an acquisition inside one scan past its timeout. Release also stops retrying once its holder record
has reached the stale threshold.
