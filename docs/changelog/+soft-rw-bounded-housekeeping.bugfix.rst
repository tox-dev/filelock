Each :class:`~filelock.SoftReadWriteLock` sweep reads at most 256 orphan holder records and 256 temporary commit files.
Cleanup resumes after the last name examined. A listing removes at most 256 obsolete generations. The lock reports a
protocol error if the generation directory contains more than 65,536 names.
