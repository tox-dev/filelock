On Windows, :class:`~filelock.StrictSoftFileLock` skips deletion of abandoned private records during acquisition.
This avoids waiting for a reader's oplock. See :ref:`how-to:Use fail-closed soft locks` for cleanup instructions.
