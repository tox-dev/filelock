Under a sticky parent such as ``/tmp``, refuse a lock file or protocol directory reported with the Linux user
namespace's overflow UID. Unmapped accounts share that reported UID, so an attacker-owned tree could otherwise be
accepted as the locking account's or the parent's and allow overlapping soft read/write or strict soft lock holders.
