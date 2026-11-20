Limit each :class:`~filelock.SoftReadWriteLock` housekeeping pass to 256 orphan holder records and 256 temporary commit
files. A user who could plant names in an attacker-writable lock tree could previously keep a nonblocking acquire
inside one sweep for seconds. Old generations are also removed 256 at a time. Listings stop after 65,537 names;
temporary commit files visible within that limit can still be collected when ``gen`` is overfull.
