A lock finalized during interpreter shutdown no longer prints an ignored ``AttributeError`` from
``BaseFileLock.__del__``: the finalizer no longer reads module globals that teardown may already have set to ``None``.
