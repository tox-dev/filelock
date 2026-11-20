Another thread calling ``release(force=True)`` on a thread-local lock no longer drops the main thread's hold during
interpreter shutdown. The hold remains until the main thread releases it, including from an ``atexit`` callback.
