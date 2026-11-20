Keep each Windows private link directory pinned until SQLite opens its database handle, so a rename cannot redirect the
connection after the link is made.
