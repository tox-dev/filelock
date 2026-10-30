Fix :class:`ReadWriteLock <filelock.ReadWriteLock>` failing every acquire with "link directory ... was replaced while
opening" in a group-writable directory whose inherited ACL, such as an NFSv4 one on ZFS, opens the link directory to
the group. The lock now closes the empty link directory to its owner, and where the filesystem refuses that, such as
ZFS with ``aclmode=restricted``, it names the directory to make writable by its owner only.
