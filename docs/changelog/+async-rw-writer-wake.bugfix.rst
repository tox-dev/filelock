``AsyncReadWriteLock`` and ``AsyncSoftReadWriteLock`` wake one waiting writer per task handoff, preventing a growing
write backlog from repeated retries by every queued writer.
