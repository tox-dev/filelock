:meth:`AsyncReadWriteLock.close() <filelock.AsyncReadWriteLock.close>` and
:meth:`AsyncSoftReadWriteLock.close() <filelock.AsyncSoftReadWriteLock.close>` now refuse a backend grant that arrives
after close starts when the close succeeds. The waiting task raises the closed-lock error before entering its block;
previously, ``close()`` released the hold under it. If close fails with the backend still held, the acquire keeps that
hold so the task can release it.
