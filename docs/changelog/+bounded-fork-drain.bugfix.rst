``os.fork()`` no longer waits forever for a lock transition another thread never finishes; after 30 seconds the child exits with status 70 and the parent continues.
