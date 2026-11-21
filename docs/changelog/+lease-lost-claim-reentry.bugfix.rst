``SoftFileLease`` and ``AsyncSoftFileLease`` reject reentrant acquisition after the heartbeat detects a lost claim.
Release the lost hold before acquiring again.
