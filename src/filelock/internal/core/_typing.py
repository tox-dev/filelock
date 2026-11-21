from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing_extensions import override
elif sys.version_info >= (3, 12):  # pragma: >=3.12 cover
    from typing import override
else:  # pragma: <3.12 cover
    from typing import TypeVar

    _Method = TypeVar("_Method")

    def override(method: _Method) -> _Method:
        """
        Mark an override on Python 3.11 without a runtime dependency.

        :param method: overriding method
        :returns: the same method
        """
        return method


__all__ = [
    "override",
]
