Py-FileLock
===========

This package contains a single module, which implements a platform independent
file locking mechanism for Python.

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


Documentation
-------------

The full documentation is available on
`readthedocs.org <https://filelock.readthedocs.org/>`_.


Contributions
-------------

Contributions are always welcome. Never hesitate to open a new issue.


License
-------

This package is `public domain <LICENSE.rst>`_.
