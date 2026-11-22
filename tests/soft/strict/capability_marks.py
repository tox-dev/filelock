from __future__ import annotations

import os
import sys
from typing import Final

import pytest
from capabilities import CAPABILITIES

NEEDS_DIR_FD: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    os.open not in os.supports_dir_fd,
    reason="injects a fault into the dir_fd cleanup path, which a runtime without dir_fd never executes",
)


NEEDS_LINK_DIR_FD: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    not CAPABILITIES["link-dir-fd"],
    reason="counts the directory the dir_fd link path opens, which a runtime without link dir_fd never opens",
)


WINDOWS_ONLY: Final[pytest.MarkDecorator] = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


CLAIM_LINK_ONLY: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    sys.platform == "win32", reason="Windows publishes claims by renaming an open handle"
)

__all__ = [
    "CLAIM_LINK_ONLY",
    "NEEDS_DIR_FD",
    "NEEDS_LINK_DIR_FD",
    "WINDOWS_ONLY",
]
