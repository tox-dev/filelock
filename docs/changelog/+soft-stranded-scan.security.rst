Bound the recovery scan used by :class:`~filelock.SoftFileLock`, :class:`~filelock.MarkerSoftFileLock` and
:class:`~filelock.SoftFileLease` to 4,096 directory entries and 64 recovery records per poll. A process that could
create names beside a lock could previously hold a nonblocking acquire past its timeout by planting many
``<lock>.restore.*`` records. A scan that exceeds either limit now treats the marker as held until the directory is
cleaned.
