Fix :class:`~filelock.SoftFileLock`, :class:`~filelock.MarkerSoftFileLock` and :class:`~filelock.SoftFileLease`
raising ``OverflowError`` out of ``acquire()``, or never reclaiming a dead holder's lock, when a stranded restore claim
beside the lock names a process number outside ``1..2**31-1`` or spells it in non-ASCII digits. Such a claim is now
ignored, as no filelock writes one.
