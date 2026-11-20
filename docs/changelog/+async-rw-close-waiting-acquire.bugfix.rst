If an async reader/writer lock grants an acquire while its wrapper is closing, the acquiring task waits for the
outcome. A successful close releases the hold and the acquire raises the closed error. If close fails with the lock
still held, the task receives its hold and can release it.
