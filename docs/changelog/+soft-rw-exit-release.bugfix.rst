:class:`~filelock.SoftReadWriteLock` and :class:`~filelock.AsyncSoftReadWriteLock` no longer release holds during
``atexit``. A daemon thread or an earlier registered exit callback could still use a hold after the callback released
it. A hold left published at process exit now ends by stale eviction; call ``release()`` or ``close()`` to hand it over
without waiting.
