When a peer publishes first, ``SoftReadWriteLock`` pauses for up to 10 ms before retrying the withdrawal of its holder
record. This reduces filesystem traffic under contention.
