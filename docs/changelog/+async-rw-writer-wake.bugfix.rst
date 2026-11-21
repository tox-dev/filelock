Async reader/writer wrappers that share a lock instance in one process grant queued writers access in arrival order.
Returning writers join the back of the queue, and new readers wait until queued writers have had their turn.
