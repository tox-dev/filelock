:class:`~filelock.StrictSoftFileLock` creates sentinels and claims without group or other write permissions. Run
``chmod go-w`` on older writable sentinels. On Linux, a contender waits if ``fs.protected_hardlinks`` prevents it from
probing another account's claim.
