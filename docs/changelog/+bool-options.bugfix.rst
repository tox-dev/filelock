Lock constructors raise :class:`TypeError` when ``thread_local``, ``is_singleton``, ``fallback_to_soft``,
``preserve_lock_file`` or ``run_in_executor`` is not a :class:`bool`. Before, the value's truthiness decided the option,
so ``is_singleton="false"`` enabled the singleton cache.
