``AsyncSoftReadWriteLock.close()`` now stops a pending acquire through that wrapper instead of waiting for another
holder to release its lock.
