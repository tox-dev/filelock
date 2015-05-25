Py-FileLock
===========

This package contains a single module, which implements a platform independent
file locking mechanism for Python.

The lock includes a lock counter and is thread safe. This means, that when
you lock the same lock object (in the same application) twice, you will get
no timeout error.


Examples
--------

.. code-block:: python

	import filelock

	lock = filelock.FileLock("my_lock_file")

	# This is the easiest way to use the file lock. Note, that the FileLock
	# object blocks until the lock can be acquired.
	with lock:
		print("Doing awesome stuff")

	# If you don't want to wait an undefined time for the file lock, you can use
	# the *acquire_* method to provide a *timeout* paramter:
	# Please note, that there is a difference between *acquire* and *acquire_*!
	# *acquire_* is made to be used in a with statement, while *acquire* is not.
	try:
		with lock.acquire_(timeout=10):
			print("Doing more awesome stuff!")
	except filelock.Timeout as err:
		print("Could not acquire the file lock. Leaving here!")
		exit(1)

	# When you're using Python 3.3+, *filelock.Timeout* is a subclass of
	# *TimeoutError* else OSError. So you can do this too:
	try:
		with lock.acquire_(timeout=10):
			print("Doing more awesome stuff!")
	except TimeoutError as err:
		print("Could not acquire the file lock. Leaving here!")
		exit(1)

	# If you don't want to use or if you can't use the *with-statement*, the
	# example above is equal to this one:
	# Note, that I am using *acquire* and not *acquire_* here.
	lock.acquire(timeout=10)
	try:
		print("Doing awesome stuff ...")
	finally:
		lock.release()

	# You can even nest the lock or acquiring it multiple times in the same
	# application.
	with lock:
		assert lock.is_locked
		with lock:
			assert lock.is_locked
		assert lock.is_locked
	assert (not lock.is_locked)


License
-------

This package is `public domain <LICENSE.rst>`_.
