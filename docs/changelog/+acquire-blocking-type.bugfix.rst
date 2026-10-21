Fix ``acquire()`` silently accepting a non-``bool`` *blocking*: a truthy value such as the string ``"false"`` was taken as
``True``, so a call that asked for a single attempt waited for the instance's ``timeout`` instead, and with the default
``timeout=-1`` it waited without a limit. The sync and the async ``acquire()`` now raise the same :class:`TypeError` that
the constructor and the ``blocking`` setter already raise.
