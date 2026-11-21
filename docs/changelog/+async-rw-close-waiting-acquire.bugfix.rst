If an async reader/writer acquire succeeds during close, it waits for close to finish. A successful close releases the
hold and the acquire raises the closed error. If close fails while the lock is held, the task receives the hold.
