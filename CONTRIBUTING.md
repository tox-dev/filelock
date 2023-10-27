# Contributing

This page lists the steps needed to set up a development environment and contribute to the project.

1. Fork and clone this repo

2. Install `tox` via `pipx`: https://tox.wiki/en/4.11.3/installation.html#via-pipx

3. Run tests

   ```shell
   tox run
   ```

   or for a specific python version

   ```shell
   tox run -f py311
   ```

4. Running other tox commands (ex. linting)

   ```shell
   tox -e fix
   ```
