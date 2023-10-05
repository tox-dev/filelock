from __future__ import annotations

from functools import wraps


class AsyncContextDecorator:
    """Vendored from https://github.com/python/cpython/blob/cf6f23b0e3cdef33f23967cf954a2ca4d1fa6528/Lib/contextlib.py#L89-L102."""

    "A base class or mixin that enables async context managers to work as decorators."

    def _recreate_cm(self):
        """Return a recreated instance of self."""
        return self

    def __call__(self, func):
        @wraps(func)
        async def inner(*args, **kwds):
            async with self._recreate_cm():
                return await func(*args, **kwds)

        return inner
