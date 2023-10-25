# Contributing

This page lists the steps needed to set up a development environment and contribute to the project.

1. Fork and clone this repo

2. Install initial dependencies in a virtual environment:

   ```shell
   python -m venv venv
   source venv/bin/activate
   python -m pip install --upgrade pip 'tox>=4.2'
   ```

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
