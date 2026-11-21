On Windows, :class:`~filelock.StrictSoftFileLock` observes the acquisition timeout while pinning claim directories and
publishing claims. An interrupted publication can leave a private record that needs manual removal. If Windows delays
withdrawal after a lost race, a worker finishes cleanup so peers can acquire after the caller times out. Release
withdraws both public claims through their original handles and waits for Windows to close them.
