A lock still referenced when the interpreter exits is released from an ``atexit`` handler before module teardown,
except holds of daemon threads that are still running, and a ``thread_local=False`` lock while any other thread runs.
