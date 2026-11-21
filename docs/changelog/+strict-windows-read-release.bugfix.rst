On Windows, a stalled :class:`~filelock.StrictSoftFileLock` claim publication now observes the acquisition deadline.
A canceled attempt can leave private records that need manual removal. Release withdraws both public claims through their
original handles. A slow filesystem or a reader holding an oplock can delay withdrawal or handle closure.
