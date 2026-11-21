Each default ``AsyncSoftReadWriteLock`` uses a private worker so a contended acquire cannot delay releases on unrelated
locks. Awaiting ``close()`` waits for that worker to exit.
