``AsyncSoftReadWriteLock.close()`` now stops a backend acquire while another holder still blocks it, instead of
waiting for that holder to release its lock.
