``SoftReadWriteLock`` and ``AsyncSoftReadWriteLock`` keep the first generation occupied when another reader extends it.
A delayed writer could previously reuse that generation while readers held the lock. A participant that sees a log reset
after staging its snapshot retries against the new log before entering.
