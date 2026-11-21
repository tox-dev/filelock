Prevent another account from redirecting :class:`ReadWriteLock <filelock.ReadWriteLock>` through a watched directory
swapped into a shared, non-sticky lock parent. The SQLite hard link now uses a fresh private directory below the
replaceable directory, keeping its final name hidden from a watch opened before the swap. Dead-holder cleanup still
recognizes link layouts left by older releases.
