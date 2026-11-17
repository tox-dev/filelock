On Windows, lock paths that differ only in drive letter or UNC share case, such as ``C:\work.lock`` and
``c:\work.lock``, now share one singleton and trigger same-thread deadlock detection.
