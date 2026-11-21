Closing an async reader/writer wrapper wakes tasks waiting on that wrapper even if another singleton wrapper holds the
lock.
