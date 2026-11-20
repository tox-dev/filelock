``SoftReadWriteLock`` and ``AsyncSoftReadWriteLock`` keep the first generation occupied when another reader extends
it. A delayed writer could previously reuse that generation while readers held the lock. If the log's epoch changes
during a commit, the participant reads the current log before entering.
