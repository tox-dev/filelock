A failed lock acquisition rolls back the level it was taking. During a synchronous acquire, calling ``release()`` from
a hook or signal handler to drop that level raises :exc:`RuntimeError`. A balanced nested ``with lock:`` remains valid.

An interrupted release on a shared instance can only retry the hold it started releasing.
