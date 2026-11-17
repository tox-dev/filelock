:class:`~filelock.SoftReadWriteLock` on runtimes without ``dir_fd`` other than Windows, such as GraalPy, now refuses a
protocol directory another account owns under a sticky parent, as the other platforms already do.
