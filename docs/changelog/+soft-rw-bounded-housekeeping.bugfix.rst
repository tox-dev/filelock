Limit each :class:`~filelock.SoftReadWriteLock` housekeeping pass to 256 orphan holder records and 256 temporary commit
files. A user who could plant names in an attacker-writable lock tree could previously keep a nonblocking acquire
inside one sweep for seconds. Old generations are also removed 256 at a time. Directory listings stop after 65,537
names; an overlarge ``gen`` directory raises :class:`~filelock.SoftFileLockProtocolError` rather than using a partial
generation list.
