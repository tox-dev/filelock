Outside Windows, ``StrictSoftFileLock`` refuses an unrelated directory renamed to a protocol path when its entries
show that it is not the lock's own. It leaves that directory's mode and contents as found instead of narrowing its mode
or creating ``claims`` inside it.
