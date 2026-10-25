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
   tox run -e 3.11
   ```

1. Running other tox commands (eg. linting):

   ```shell
   tox -e fix
   ```

## Release notes

Add a `docs/changelog/+short-name.<type>.rst` fragment for each change that affects library users. The name describes
the change; it does not need an issue or pull request number. Leave out credits and pull request links: the news check
rejects a fragment that carries either. Pending fragments appear under Unreleased in documentation builds. They enter
`docs/changelog.rst` only when a release is prepared.

| Type          | Section          | Use for                                                    |
| ------------- | ---------------- | ---------------------------------------------------------- |
| `feature`     | Added            | New capabilities and Python support additions or removals  |
| `change`      | Changes          | Changed behavior that remains available                    |
| `bugfix`      | Fixed            | Corrected behavior                                         |
| `doc`         | Documentation    | Documentation changes users need to know about             |
| `breaking`    | Breaking changes | Changes that require users to update code or configuration |
| `security`    | Security         | Security fixes users need to act on                        |
| `performance` | Performance      | User-visible speed or resource improvements                |
| `deprecation` | Deprecated       | Supported behavior scheduled for removal                   |
| `removal`     | Removed          | Removed user-facing capabilities other than Python support |
| `packaging`   | Packaging        | Install, wheel, or distribution changes                    |

Apply `skip news` to a pull request with no user-facing change, including maintenance work. The label may be applied
after the pull request opens. Dependabot, pre-commit.ci, and repository automation receive it when they supply no
fragment. A bot pull request gets no release entry unless it supplies one.

Credits come from the commits. When a release is prepared, each fragment is linked to its squash-merged pull request and
credited to the pull request author, then every other author and `Co-authored-by` co-author of the merge commit. Bots
are credited only when no human author remains. After the changelog is built, prepare runs
`release-creator contributors`, which adds a New Contributors subsection for first-time pull request authors.

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
