:class:`~filelock.SoftReadWriteLock` scans up to 256 orphan holder records and 256 temporary files per sweep, resuming
where it stopped. Each listing removes up to 256 obsolete generations. The lock raises a
protocol error above 65,536 generation entries.
