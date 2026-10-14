# filelock installed from source with its test dependency group, the way the tox test environments install it. The
# python image ships git and a C toolchain; 3.14 is the newest CPython the project supports with a stable image.
FROM python:3.14-bookworm
ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Two unprivileged accounts, so that attacks by another local user on a shared directory such as /tmp can be run with
# runuser instead of as root, which bypasses the permission checks the lock relies on.
RUN useradd --create-home victim && useradd --create-home attacker

# A virtual environment on PATH, so that python and pytest work in every shell, interactive or not.
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

# scanner contract: the checkout lives inside the image, at /src
COPY . /src
WORKDIR /src

# hatch-vcs takes the version from git tags; a checkout without them gets a placeholder version instead of failing.
RUN pip install --upgrade pip \
    && { git describe --tags >/dev/null 2>&1 || export SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0; } \
    && pip install -e . --group test

# The oldest legacy client the StrictSoftFileLock compatibility tests run against, fetched now because the scan has no
# network. The tox 3.15 environment installs it the same way.
RUN pip install --no-deps --target /opt/filelock-3.20.0 filelock==3.20.0
ENV FILELOCK_OLD_CLIENT_PATH=/opt/filelock-3.20.0

# The source tree must stay readable to the unprivileged accounts above.
RUN chmod -R a+rX /src /opt/venv /opt/filelock-3.20.0

# Run the test suite so you know the image works, but do not let a failing test abort the build.
RUN python -m pytest tests -q || echo "WARNING: pytest reported failures"
