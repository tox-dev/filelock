``SoftReadWriteLock`` and ``AsyncSoftReadWriteLock`` limit generation scans during acquire. Release raises a protocol
error if it cannot withdraw the holder before its stale threshold.
