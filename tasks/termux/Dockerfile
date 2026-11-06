# Cached base image for the Termux (Android/bionic) test run: only the expensive, source-independent setup lives here,
# so iterating on filelock re-runs just the copy, the local install and the tests (see compose.yaml), never this
# bootstrap. termux-docker's apt refuses to run as root and only works after its bootstrap, which lives in the image
# entrypoint, so the build step is dispatched through /entrypoint.sh. compose.yaml copies the source into $HOME; that
# copy preserves the mount's host ownership, so git is told to trust the tree for hatch-vcs' version lookup.
# requirements.txt pins tox by hash; its header holds the command that regenerates it.
FROM termux/termux-docker:aarch64@sha256:18e2c6a633642d0806afd7abaeb0e74b2b957c92f6f30021260e5ba997579f48
COPY requirements.txt /tmp/requirements.txt
RUN ["/entrypoint.sh", "bash", "-lc", "apt-get update -qq && apt-get install -yq python python-pip git && pip install -q --require-hashes -r /tmp/requirements.txt && git config --system --add safe.directory '*'"]
