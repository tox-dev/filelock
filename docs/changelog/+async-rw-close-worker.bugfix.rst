Closing an ``AsyncReadWriteLock`` now joins its worker without starting a thread in the event loop's default executor.
Close also succeeds after the default executor has shut down.
