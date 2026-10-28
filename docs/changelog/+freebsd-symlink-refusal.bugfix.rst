Handle ``EMLINK``, the error FreeBSD reports when a no-follow open refuses a symlink, in a soft reader/writer lock's
protocol tree, so heartbeat reports lost ownership there rather than a transient refresh error.
