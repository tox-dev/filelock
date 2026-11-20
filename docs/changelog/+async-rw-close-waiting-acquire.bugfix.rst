:meth:`AsyncReadWriteLock.close() <filelock.AsyncReadWriteLock.close>` and
:meth:`AsyncSoftReadWriteLock.close() <filelock.AsyncSoftReadWriteLock.close>` now refuse a backend grant that arrives
after close starts. The waiting task raises the closed-lock error before entering its block; previously, ``close()``
released the hold under it.
