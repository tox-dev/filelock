Keep soft locks from breaking a live holder's marker when two Linux processes share a lock path but see different boot
IDs because one sees a file mounted over ``/proc/sys/kernel/random/boot_id``. A boot ID from a mounted-over path now
counts as unavailable, so contenders compare the process start time alone.
