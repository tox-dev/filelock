import os
import stat
import sys

PermissionError = PermissionError if sys.version_info[0] == 3 else OSError


def raise_on_exist_ro_file(filename):
    try:
        file_stat = os.stat(filename)  # use stat to do exists + can write to check without race condition
    except OSError:
        pass  # swallow does not exist or other errors
    else:
        if not (file_stat.st_mode & stat.S_IWUSR):
            info = {
                k: getattr(file_stat, k)
                for k in dir(file_stat)
                if not k.startswith("__") and not callable(getattr(file_stat, k))
            }
            raise PermissionError("Permission denied: {!r} with {}".format(filename, info))


__all__ = [
    "raise_on_exist_ro_file",
    "PermissionError",
]
