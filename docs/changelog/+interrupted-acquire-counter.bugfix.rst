An exception raised during early :meth:`~filelock.BaseFileLock.acquire` no longer leaves a count on an unheld lock.
Reusing that instance previously left its next ``with`` block holding the backend lock after exit. A failed nested
acquire keeps the outer count, and async acquisition follows the same rollback rule.
