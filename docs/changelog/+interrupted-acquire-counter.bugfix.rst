A failed acquire rolls back its own nesting level. Releasing that level from a hook or signal handler raises
:exc:`RuntimeError`. Balanced nested holds still work. Retrying an interrupted release on a shared instance affects
only the original hold. An indefinite acquire raises :exc:`RuntimeError` if a signal handler returns holding the same
path through another instance.
