Fix the read-only reopen of a lock file this account cannot write resolving a relative path against the working
directory of the moment. A ``chdir`` between the refused ``O_RDWR`` open and the reopen made the acquisition wait on a
file that is not there, or probe one in another directory. The reopen now uses the path the acquisition pinned.
