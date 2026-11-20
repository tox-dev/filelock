Fix forked file and soft reader/writer locks releasing their parent's hold when both processes have the same numeric PID
in different Linux PID namespaces. Inherited locks are invalidated even when those PIDs match.
