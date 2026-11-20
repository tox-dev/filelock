An indefinite acquire now raises :class:`RuntimeError` if a signal handler releases its provisional hold and acquires
the same path through another instance in the same thread. Finite and nonblocking acquires retain their timeout behavior.
