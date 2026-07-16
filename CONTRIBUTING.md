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
