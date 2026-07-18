# Cached base image for the Termux (Android/bionic) test run: only the expensive, source-independent setup lives here,
# so iterating on filelock re-runs just the copy, the local install and the tests (see compose.yaml), never this
# bootstrap. termux-docker's apt refuses to run as root and only works after its bootstrap, which lives in the image
# entrypoint, so the build step is dispatched through /entrypoint.sh. compose.yaml copies the source into $HOME; that
# copy preserves the mount's host ownership, so git is told to trust the tree for hatch-vcs' version lookup.
FROM termux/termux-docker:aarch64
RUN ["/entrypoint.sh", "bash", "-lc", "apt-get update -qq && apt-get install -yq python python-pip git && pip install -q tox && git config --system --add safe.directory '*'"]
