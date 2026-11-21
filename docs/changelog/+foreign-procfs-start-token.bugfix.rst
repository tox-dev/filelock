On Linux, :class:`~filelock.SoftFileLock`, :class:`~filelock.MarkerSoftFileLock` and :class:`~filelock.SoftFileLease`
use only the PID to check liveness when the holder or contender sees another PID namespace's ``/proc``. This preserves
live markers.
