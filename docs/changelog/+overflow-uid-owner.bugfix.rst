Under a sticky parent such as ``/tmp``, refuse a lock file or protocol directory reported with Linux's overflow UID.
User namespaces and idmapped mounts can report unrelated owners with that UID, so an attacker-owned tree could
otherwise be accepted as the locking account's or the parent's and allow overlapping lock holders.
