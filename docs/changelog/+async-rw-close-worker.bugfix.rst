Closing an ``AsyncReadWriteLock`` now joins its worker without starting a thread in the event loop's default executor.
Close also succeeds after the default executor has shut down. For both async reader/writer locks, a second close waits
for the worker to exit even if the first close was canceled.
