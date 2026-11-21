:class:`ReadWriteLock <filelock.ReadWriteLock>` prevents database redirection in shared, non-sticky directories by
placing its hard link in a fresh private directory. Cleanup recognizes older link layouts.
