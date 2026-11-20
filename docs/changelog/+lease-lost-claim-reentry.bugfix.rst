``SoftFileLease`` and ``AsyncSoftFileLease`` now refuse a reentrant acquire after their heartbeat reports a lost claim.
Release the lost hold before acquiring again. Previously, another task or thread sharing the instance could join that
hold while a peer owned the lease.
