:class:`~filelock.asyncio.BaseAsyncFileLock` now invokes the :meth:`~filelock.BaseFileLock._poll_delay` hook
and tracks attempt counts during contended acquisitions, ensuring consistent backoff behavior and hook customization across sync and async locks.
