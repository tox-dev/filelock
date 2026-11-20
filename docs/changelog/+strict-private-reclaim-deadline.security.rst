On Windows, :class:`~filelock.StrictSoftFileLock` leaves abandoned private records in place during a claim scan.
The scan can then continue without waiting for a reader's oplock on those records.
See :ref:`how-to:Use fail-closed soft locks` for cleanup instructions.
