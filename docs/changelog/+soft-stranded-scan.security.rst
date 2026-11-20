Bound the recovery scan for soft locks in shared directories. A local user could plant enough adjacent names to make a
nonblocking acquire spend seconds scanning before it returned. Recovery now treats an oversized directory as held.
