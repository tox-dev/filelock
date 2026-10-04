``ReadWriteLock`` and ``AsyncReadWriteLock`` use a validated private hard link when ``/dev/fd`` has no entry for the
database descriptor, supporting NetBSD's static descriptor directory beyond descriptor 63. The temporary location must
share a filesystem with the database. The symlink refusal test accepts NetBSD's error wording.
