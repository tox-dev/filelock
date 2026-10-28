# Security policy

filelock is a Python library for mutual exclusion between cooperating processes through a lock file. This policy says
how to report a security problem and how we decide whether a report is one.

## Reporting a vulnerability

**Do not open a public issue for a security problem.** Use
[GitHub's private vulnerability reporting](https://github.com/tox-dev/filelock/security/advisories/new). If you do not
have a GitHub account, or you are a Tidelift subscriber, report through the
[Tidelift security contact](https://tidelift.com/security) instead; Tidelift coordinates the fix and the disclosure with
us. If neither works for you, email [gaborjbernat@gmail.com](mailto:gaborjbernat@gmail.com).

Include what the attacker controls and what they gain, the steps or a proof of concept, and the versions and platform
you tested. Write the report yourself; unreviewed tool or model output takes longer to triage.

## What to expect

Volunteers maintain filelock, with no on-call rotation. We aim to acknowledge a report within **5 working days** and
assess it within **15 working days**. Fixes are developed in a private fork attached to the draft advisory, ship in a
new release, and the advisory, with a CVE requested through GitHub, goes out with that release. The default embargo is
**90 days** from the report; we agree any change with the reporter. We credit reporters in the advisory under the name
they choose, or not at all. There is no bug bounty.

## Supported versions

Only the latest release receives fixes. Confirm the problem reproduces on it before reporting.

## Vulnerability or bug

A report is a vulnerability when an attacker who controls only an input that the [threat model](THREAT_MODEL.md) lists
as untrusted breaks a guarantee filelock makes. A report that first needs control of something the threat model trusts,
such as a process running under the lock holder's user ID, is a bug: we fix it in the open and publish no advisory.
Classify by who supplies the dangerous input, not by how bad the outcome sounds.

**In scope**

- Another local user making a lock open, create, truncate, `chmod`, rename, unlink or delete a file other than the
  intended lock file, through a symlink, hard link, planted node or a race at the lock path or any name filelock derives
  from it.
- Two processes holding the same exclusive lock at once with no `lifetime`, lease expiry or `on_compromise` signal,
  including a forged marker or claim that makes a live holder look stale.
- A lock file created with wider permissions than the `mode` the caller asked for.
- A crash, hang or unbounded resource use caused by crafted lock file contents or a planted FIFO.
- Another user reading data from the victim's files through the lock, for example through an error message.
- Denial of service by a local user who holds the lock, creates the lock file first, or leaves a claim behind; expected
  of an advisory lock in a shared directory, so rated Low.
- Windows-only issues, with the same ratings.

**Out of scope**

- Anything a process with the holder's user ID does: it can rewrite or delete any lock file by design.
- Processes that ignore the lock; advisory locks do not stop direct access to the protected resource.
- Overlap the API documents: `SoftFileLock` with `lifetime`, `SoftFileLease` expiry, `SoftReadWriteLock` eviction,
  `break_lock()` and `force_break()`.
- A lock file another user created first keeping that user's ownership and mode.
- Misuse that filelock reports as an error: an instance used after `fork()`, a WAL-mode database, a directory at the
  lock path.
- Attacker-writable directories above the lock's parent, and network filesystem semantics such as `flock` over NFS.

If you cannot tell which side a finding falls on, report it privately and we will decide.
