Reject blocking descriptor-lock polling intervals above :data:`threading.TIMEOUT_MAX` before attempting the lock,
instead of raising ``OverflowError`` only when the descriptor is contended. Nonblocking calls still ignore the interval.
