On Windows, :class:`~filelock.StrictSoftFileLock` skips abandoned private records to avoid waiting on reader oplocks.
For manual cleanup, see :ref:`how-to:Use fail-closed soft locks`.
