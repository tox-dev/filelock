import os
import stat
import sys

PermissionError = PermissionError if sys.version_info[0] == 3 else OSError


def raise_on_exist_ro_file(filename):
    try:
        file_stat = os.stat(filename)  # use stat to do exists + can write to check without race condition
    except OSError:
        pass
    else:
        if not (file_stat.st_mode & stat.S_IWUSR):
            raise PermissionError("Permission denied: {!r} read-only".format(filename))


__all__ = [
    "raise_on_exist_ro_file",
    "PermissionError",
]
