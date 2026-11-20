A successful close of an ``AsyncReadWriteLock`` or ``AsyncSoftReadWriteLock`` wrapper now wakes its task-table waiters
while another singleton wrapper still holds the lock.
