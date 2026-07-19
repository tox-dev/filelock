# Contributing

This page lists the steps needed to set up a development environment and contribute to the project.

1. Fork and clone this repo.

1. [Install tox](https://tox.wiki/en/latest/installation.html#via-pipx).

1. Run tests:

   ```shell
   tox run
   ```

   or for a specific python version

   ```shell
   tox run -f py311
   ```

1. Running other tox commands (eg. linting):

   ```shell
   tox -e fix
   ```

## Coverage

Every environment in the matrix must reach 100%, and so must the combined report. Name the capability a line needs
rather than the platform that happens to lack it. A platform name goes stale. One `win32 no cover` sat on the `os.link`
fallback for `follow_symlinks` and demanded a branch modern Windows never takes, and others excluded platform-agnostic
code and hid Windows gaps behind it.

`tasks/coverage_pragmas.py` probes each capability at runtime and drives both directions.

- `# pragma: needs <capability>` drops the line only where the capability is absent.
- `# pragma: lacks <capability>` drops it only where the capability is present.

Tests gate their `skipif` on the same `CAPABILITIES` mapping, so a test cannot be skipped while coverage still demands
its lines. Reach for a capability wherever one fits. `hard-link` is present on Windows, which `win32 no cover` could not
express, and `symlink` is a privilege a Windows runner may hold. Add a new one by giving it a probe in that mapping;
where a missing capability makes a whole module unrunnable, list the module under `_CAPABILITY_MODULES` instead of
marking every line in it.

Cover a line rather than excluding it wherever it is reachable, and prefer a test that drives a path directly over one
that waits for a finalizer or a background thread to reach it, since coverage that depends on timing turns a 100% gate
into a flaky build.

PyPy runs unmeasured. Coverage has no JIT path there, so the suite takes 12m rather than 2m and roughly 60 of its lock,
heartbeat and poll deadlines lapse under the added latency, though the same tests pass measured in isolation. Meeting
the gate there would mean inflating those deadlines everywhere to satisfy an environment whose data is neither uploaded
nor combined.
