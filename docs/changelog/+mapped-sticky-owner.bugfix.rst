On Linux, an unprivileged account can use its own lock in a sticky directory when its UID matches the kernel's overflow
UID or procfs is unavailable. This includes container lock files on OverlayFS. On unsupported filesystems, use a
private lock directory if ownership cannot be verified.
