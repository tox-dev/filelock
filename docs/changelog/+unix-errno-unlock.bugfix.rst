Retry Unix unlock once after an errno-bearing release error. If a signal arrives after the kernel unlock, a successful retry clears the stale local hold.
