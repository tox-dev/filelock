On POSIX, :class:`~filelock.SoftFileLease` records claim loss detected during release in ``compromise``. Only the
heartbeat thread calls ``on_compromise``.
