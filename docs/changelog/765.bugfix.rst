Reject conflicting ``loop``, ``executor``, and ``run_in_executor`` options when reusing an asynchronous singleton lock,
instead of silently ignoring the new values.
