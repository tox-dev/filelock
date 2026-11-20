``SoftReadWriteLock`` and ``AsyncSoftReadWriteLock`` keep a generation record when another participant may have
built on it. At a cold start, a reader could remove its own record after a peer extended it, freeing
the first generation for a delayed writer and allowing a writer beside readers.
