Changelog
=========
in progress
-----------
- Use ``time.perf_counter`` instead of ``time.monotonic`` for calculating timeouts.

v3.9.0 (2022-12-28)
-------------------
- Move build backend to ``hatchling`` :pr:`185 - by :user:`gaborbernat`.

v3.8.1 (2022-12-04)
-------------------
- Fix mypy does not accept ``filelock.FileLock`` as a valid type

v3.8.0 (2022-12-04)
-------------------
- Bump project dependencies
- Add timeout unit to docstrings
- Support 3.11

v3.7.1 (2022-05-31)
-------------------
- Make the readme documentation point to the index page

v3.7.0 (2022-05-13)
-------------------
- Add ability to return immediately when a lock cannot be obtained

v3.6.0 (2022-02-17)
-------------------
- Fix pylint warning "Abstract class :class:`WindowsFileLock <filelock.WindowsFileLock>` with abstract methods instantiated"
  :pr:`135` - by :user:`vonschultz`
- Fix pylint warning "Abstract class :class:`UnixFileLock <filelock.UnixFileLock>` with abstract methods instantiated"
  :pr:`135` - by :user:`vonschultz`

v3.5.1 (2022-02-16)
-------------------
- Use ``time.monotonic`` instead of ``time.time`` for calculating timeouts.

v3.5.0 (2022-02-15)
-------------------
- Enable use as context decorator

v3.4.2 (2021-12-16)
-------------------
- Drop support for python ``3.6``

v3.4.1 (2021-12-16)
-------------------
- Add ``stacklevel`` to deprecation warnings for argument name change

v3.4.0 (2021-11-16)
-------------------
- Add correct spelling of poll interval parameter for :meth:`acquire <filelock.BaseFileLock.acquire>` method, raise
  deprecation warning when using the misspelled form :pr:`119` - by :user:`XuehaiPan`.

v3.3.2 (2021-10-29)
-------------------
- Accept path types (like ``pathlib.Path`` and ``pathlib.PurePath``) in the constructor for ``FileLock`` objects.

v3.3.1 (2021-10-15)
-------------------
- Add changelog to the documentation :pr:`108` - by :user:`gaborbernat`
- Leave the log level of the ``filelock`` logger as not set (previously was set to warning) :pr:`108` - by
  :user:`gaborbernat`

v3.3.0 (2021-10-03)
-------------------
- Drop python 2.7 and 3.5 support, add type hints :pr:`100` - by :user:`gaborbernat`
- Document asyncio support - by :user:`gaborbernat`
- fix typo :pr:`98` - by :user:`jugmac00`

v3.2.1 (2021-10-02)
-------------------
- Improve documentation
- Changed logger name from ``filelock._api`` to ``filelock`` :pr:`97` - by :user:`hkennyv`

v3.2.0 (2021-09-30)
-------------------
- Raise when trying to acquire in R/O or missing folder :pr:`96` - by :user:`gaborbernat`
- Move lock acquire/release log from INFO to DEBUG :pr:`95` - by :user:`gaborbernat`
- Fix spelling and remove ignored flake8 checks - by :user:`gaborbernat`
- Split main module :pr:`94` - by :user:`gaborbernat`
- Move test suite to pytest :pr:`93` - by :user:`gaborbernat`

v3.1.0 (2021-09-27)
-------------------
- Update links for new home at tox-dev :pr:`88` - by :user:`hugovk`
- Fixed link to LICENSE file :pr:`63` - by :user:`sharkwouter`
- Adopt tox-dev organization best practices :pr:`87` - by :user:`gaborbernat`
- Ownership moved from :user:`benediktschmitt` to the tox-dev organization (new primary maintainer :user:`gaborbernat`)

v3.0.12 (2019-05-18)
--------------------
- *fixed* setuptools and twine/warehouse error by making the license only 1 line long
- *update* version for pypi upload
- *fixed* python2 setup error
- *added* test.py module to MANIFEST and made tests available in the setup commands :issue:`48`
- *fixed* documentation thanks to :user:`AnkurTank` :issue:`49`
- Update Trove classifiers for PyPI
- test: Skip test_del on PyPy since it hangs

v3.0.10 (2018-11-01)
--------------------
- Fix README rendering on PyPI

v3.0.9 (2018-10-02)
-------------------
- :pr:`38` from cottsay/shebang
- *updated* docs config for older sphinx compatibility
- *removed* misleading shebang from module

v3.0.8 (2018-09-09)
-------------------
- *updated* use setuptools

v3.0.7 (2018-09-09)
-------------------
- *fixed* garbage collection (:issue:`37`)
- *fix* travis ci badge (use rst not markdown)
- *changed* travis uri

v3.0.6 (2018-08-22)
-------------------
- *clean up*
- Fixed unit test for Python 2.7
- Added Travis banner
- Added Travis CI support

v3.0.5 (2018-04-26)
-------------------
- Corrected the prequel reference

v3.0.4 (2018-02-01)
-------------------
- *updated* README

v3.0.3 (2018-01-30)
-------------------
- *updated* readme

v3.0.1 (2018-01-30)
-------------------
- *updated* README (added navigation)
- *updated* documentation :issue:`22`
- *fix* the ``SoftFileLock`` test was influenced by the test for ``FileLock``
- *undo* ``cb1d83d`` :issue:`31`

v3.0.0 (2018-01-05)
-------------------
- *updated* major version number due to :issue:`29` and :issue:`27`
- *fixed* use proper Python3 ``reraise`` method
- Attempting to clean up lock file on Unix after ``release``

v2.0.13 (2017-11-05)
--------------------
- *changed* The logger is now acquired when first needed. :issue:`24`

v2.0.12 (2017-09-02)
--------------------
- correct spelling mistake

v2.0.11 (2017-07-19)
--------------------
- *added* official support for python 2 :issue:`20`

v2.0.10 (2017-06-07)
--------------------
- *updated* readme

v2.0.9 (2017-06-07)
-------------------
- *updated* readme :issue:`19`
- *added* example :pr:`16`
- *updated* readthedocs url
- *updated* change order of the examples (:pr:`16`)

v2.0.8 (2017-01-24)
-------------------
- Added logging
- Removed unused imports

v2.0.7 (2016-11-05)
-------------------
- *fixed* :issue:`14` (moved license and readme file to ``MANIFEST``)

v2.0.6 (2016-05-01)
-------------------
- *changed* unlocking sequence to fix transient test failures
- *changed* threads in tests so exceptions surface
- *added* test lock file cleanup

v2.0.5 (2015-11-11)
-------------------
- Don't remove file after releasing lock
- *updated* docs

v2.0.4 (2015-07-29)
-------------------
- *added* the new classes to ``__all__``

v2.0.3 (2015-07-29)
-------------------
- *added* The ``SoftFileLock`` is now always tested

v2.0.2 (2015-07-29)
-------------------
- The filelock classes are now always available and have been moved out of the
  ``if msvrct: ... elif fcntl ... else`` clauses.

v2.0.1 (2015-06-13)
-------------------
- fixed :issue:`5`
- *updated* test cases
- *updated* documentation
- *fixed* :issue:`2` which has been introduced with the lock counter

v2.0.0 (2015-05-25)
-------------------
- *added* default timeout (fixes :issue:`2`)

v1.0.3 (2015-04-22)
-------------------
- *added* new test case, *fixed* unhandled exception

v1.0.2 (2015-04-22)
-------------------
- *fixed* a timeout could still be thrown if the lock is already acquired

v1.0.1 (2015-04-22)
-------------------
- *fixed* :issue:`1`

v1.0.0 (2015-04-07)
-------------------
- *added* lock counter, *added* unittest, *updated* to version 1
- *changed* filenames
- *updated* version for pypi
- *updated* README, LICENSE (changed format from md to rst)
- *added* MANIFEST to gitignore
- *added* os independent file lock ; *changed* setup.py for pypi
- Update README.md
- initial version
