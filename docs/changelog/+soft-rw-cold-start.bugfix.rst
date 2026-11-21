``SoftReadWriteLock`` and ``AsyncSoftReadWriteLock`` preserve active readers when a delayed writer tries to reuse the
first generation. Acquires retry if the log resets before publication.
