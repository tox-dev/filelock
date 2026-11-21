``AsyncSoftReadWriteLock`` uses a private worker by default, so a blocked acquire cannot delay unrelated releases.
Awaiting ``close()`` joins the worker.
