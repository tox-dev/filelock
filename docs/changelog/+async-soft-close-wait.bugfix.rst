Closing an async reader/writer wrapper stops its contended acquires on the next poll, even if another singleton
wrapper is waiting.
