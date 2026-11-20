Linux accounts without ``CAP_FOWNER`` can use their own native, strict soft and soft read/write locks in a sticky
directory when their UID matches the kernel's overflow UID or procfs is unavailable. This includes unprivileged
containers with ``/tmp`` on OverlayFS. Use a private lock directory if the filesystem cannot verify ownership.
