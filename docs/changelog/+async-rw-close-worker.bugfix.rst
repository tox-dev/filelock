``AsyncReadWriteLock`` can close after the default executor shuts down. Closing either async reader/writer lock again
after a canceled close waits for its worker to exit.
