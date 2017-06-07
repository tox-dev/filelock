Py-FileLock
===========

This package contains a single module, which implements a platform independent
file lock in Python.

The lock includes a lock counter and is thread safe. This means, when locking
the same lock object twice, it will not block.

.. code-block:: python

	import filelock

	lock = filelock.FileLock("my_lock_file")

	with lock:
		pass

	try:
		with lock.acquire(timeout = 10):
			pass
	except filelock.Timeout:
		pass


What this *filelock* is not
---------------------------

A *filelock* provides a synchronisation mechanism between different instances
of your application, similar to a thread lock. It can be used to *signalize*
that files, directories or other resources are currently used or manipulated
(Think of a sync.lock file). Only the existence of the lockfile is watched for
this purpose. The file itself can not be written and is always empty.

 Perhaps you are looking for something like

*	https://pypi.python.org/pypi/pid/2.1.1
* https://docs.python.org/3.6/library/msvcrt.html#msvcrt.locking
* or https://docs.python.org/3/library/fcntl.html#fcntl.flock


Documentation
-------------

The full documentation is available on
`readthedocs.org <https://filelock.readthedocs.io/>`_.


Contributions
-------------

Contributions are always welcome. Never hesitate to open a new issue.


License
-------

This package is `public domain <LICENSE.rst>`_.
