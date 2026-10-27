Reopen a lock file whose ``O_RDWR`` open was refused at the path the acquisition pinned, so a ``chdir`` between the two
opens no longer probes a file in another directory or waits on one that is not there.
