Closing an ``AsyncReadWriteLock`` or ``AsyncSoftReadWriteLock`` stops its contended backend acquires on their next poll.
A singleton wrapper can close while another wrapper waits to acquire the backend.
