Fix :class:`~filelock.SoftFileLock`, :class:`~filelock.MarkerSoftFileLock` and :class:`~filelock.SoftFileLease` wedging,
or letting two processes hold the lock at once, when three or more contenders raced for a dead holder's marker. The lock
path could end up naming a contender that held nothing, which every other contender then waited for, without end at the
default ``timeout``, while the live holder's marker stayed under a break name. A contender now reads the marker again
right before it exchanges, and contenders that lose the race exchange what they took back in reverse order.
