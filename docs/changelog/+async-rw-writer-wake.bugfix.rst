Async reader/writer wrappers sharing a lock serve waiting writers in arrival order, ahead of new readers. Returning
writers rejoin at the back.
