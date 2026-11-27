A ``mode`` that equals the unset default without being an :class:`int`, such as ``-1.0``, ``Decimal(-1)`` or
``Fraction(-1)``, raises :class:`TypeError` like any other non-``int`` mode. Before, the lock silently used the default
permissions.
