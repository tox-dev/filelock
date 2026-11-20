Write the soft fallback record into the file :class:`~filelock.UnixFileLock` created, avoiding a pathname unlink race.
Refuse the downgrade when that file was group- or other-writable, since a peer may already hold a writable descriptor.
