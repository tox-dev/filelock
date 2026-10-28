A ``fork()`` child of a process holding a :class:`~filelock.SoftFileLease` starts from an unheld claim. It used to
inherit the parent's token, last compromise and heartbeat, so :attr:`~filelock.SoftFileLease.token` and
:attr:`~filelock.SoftFileLease.compromise` reported a claim the child never held while ``is_locked`` reported it unheld.
