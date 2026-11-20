On POSIX, :class:`~filelock.SoftFileLease` now records a claim taken before release even when the heartbeat has not
reported it. ``on_compromise`` remains a heartbeat-thread callback; callers can inspect ``compromise`` after release.
