from abc import ABCMeta


class BaseReadWriteFileLockWrapper(metaclass=ABCMeta):
    _read_write_file_lock_cls: Type[BaseReadWriteFileLock]

    def __init__(  # noqa: PLR0913
        self,
        lock_file: str | os.PathLike[str] | None = None,
        timeout: float = -1,
        mode: int = 0o644,
        thread_local: bool = True,  # noqa: FBT001, FBT002
        *,
        blocking: bool = True,
        lock_file_inner: str | os.PathLike[str] | None = None,
        lock_file_outer: str | os.PathLike[str] | None = None,
    ) -> None:
        """
        Convinience wrapper for read/write locks.

        See filelock.read_write.ReadWriteFileLock for description of the parameters.
        """
      self.read_lock = self._read_write_file_lock_cls(
          lock_file_inner=lock_file_inner,
          lock_file_outer=lock_file_outer,
          read_write_mode=ReadWriteMode.READ,
          timeout=timeout,
          mode=mode,
          thread_local=thread_local,
          blocking=blocking,
      )
      self.write_lock = self._read_write_file_lock_cls(
          lock_file_inner=lock_file_inner,
          lock_file_outer=lock_file_outer,
          read_write_mode=ReadWriteMode.WRITE,
          timeout=timeout,
          mode=mode,
          thread_local=thread_local,
          blocking=blocking,
      )

    def __call__(self, read_write_mode: ReadWriteMode):
        """Get read/write lock object with the specified ``read_write_mode``.

        :param read_write_mode: whether this object should be in WRITE mode or READ mode.
        :return: a lock object in specified ``read_write_mode``.
        """
        if read_write_mode == ReadWriteMode.READ:
            return self.read_lock
        elif read_write_mode == ReadWriteMode.WRITE:
            return self.write_lock


class _DisabledReadWriteFileLockWrapper(BaseReadWriteFileLockWrapper):
    def __new__(cls):
        raise NotImplementedError("ReadWriteFileLock is unavailable.")


__all__ = [
    "BaseReadWriteFileLockWrapper",
]
