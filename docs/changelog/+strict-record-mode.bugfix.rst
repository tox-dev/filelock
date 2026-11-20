:class:`~filelock.StrictSoftFileLock` no longer gives its sentinel and its claims the group or other write bits. Under
a ``002`` umask or a default ACL another account could rewrite the sentinel into a marker of an exited process, which a
:class:`~filelock.SoftFileLock` contender then removed while the strict holder was inside, or rewrite the PID a claim
reports. A contender that Linux's ``fs.protected_hardlinks`` stops from probing another account's claim now waits on
it instead of raising :class:`~filelock.SoftFileLockProtocolError`. A sentinel an earlier release created keeps its
mode; remove its group and other write bits with ``chmod go-w``.
