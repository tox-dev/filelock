During shutdown, another thread's ``release(force=True)`` preserves the main thread's thread-local hold, including
during ``atexit`` callbacks.
