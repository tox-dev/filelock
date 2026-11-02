A :class:`~filelock.SoftFileLease` reclaims a dead holder's lease marker even when that holder published another
``lease_duration``. It used to raise :class:`~filelock.LeaseSettingsMismatch` on every attempt instead, so after a
holder crashed and its peers restarted with a changed duration the lock stayed wedged until
:meth:`force_break() <filelock.MarkerSoftFileLock.force_break>`. A live holder with another duration still raises.
