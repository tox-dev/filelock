:meth:`~filelock.BaseFileLock.acquire` and :meth:`~filelock.BaseAsyncFileLock.acquire` raise :class:`TypeError` for a
*blocking* that is not a :class:`bool`, as the constructor and the ``blocking`` setter already do. A truthy value such
as the string ``"false"`` used to count as ``True``, so a call meant as a single attempt waited for the instance's
``timeout``, and without a limit at the default ``-1``.
