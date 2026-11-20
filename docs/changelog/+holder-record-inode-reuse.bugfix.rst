Fix a :class:`~filelock.SoftReadWriteLock` heartbeat writing its holder record into another file of the holder's. In a
directory another account can write, that account could remove the record and rename over its name a file the holder
created later, which ext4 gives the record's inode number. The heartbeat now also requires the file to start with the
record's own token, and reports the hold lost when it does not.
