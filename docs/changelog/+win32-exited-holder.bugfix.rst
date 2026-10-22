Fix soft locks on Windows keeping the marker of a holder that has exited while another process still holds a handle to
it, such as a parent's ``subprocess.Popen`` after ``wait()`` or ``multiprocessing.Process`` after ``join()``. Acquiring
such a lock with the default ``timeout=-1`` waited forever.
