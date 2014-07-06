# Py-FileLock
This package contains a single module [filelock](filelock.py), which implements
a platform independent file locking mechanism for Python.


## Examples
```Python
import filelock

lock = filelock.FileLock("my_lock_file")

# This is the easiest way to use the file lock. Note, that the FileLock object
# blocks until the lock can be acquired.
with lock:
	print("Doing awesome stuff")

# If you don't want to wait an undefined time for the file lock, you can use
# the *acquire* method to provide a *timeout* paramter:
try:
	with lock.acquire(timeout=10):
		print("Doing more awesome stuff!")
except filelock.Timeout as err:
	print("Could not acquire the file lock. Leaving here!")
	exit(1)

# When you're using Python 3.3+, *filelock.Timeout* is a subclass of
# *TimeoutError* else OSError. So you can do this too:
try:
	with lock.acquire(timeout=10):
		print("Doing more awesome stuff!")
except TimeoutError as err:
	print("Could not acquire the file lock. Leaving here!")
	exit(1)
	
# If you don't want to use or if you can't use the *with-statement*, the 
# example above is equal to this one:
try:
	lock.acquire(timeout=10)
except filelock.Timeout as err:
	print("Could not acquire the file lock. Leaving here!")
	exit(1)
else:
	print("Doing more awesome stuff!")
finally:
	lock.release()
```

	
## License
This package is [public domain](LICENSE.md).
