Linux accounts without ``CAP_FOWNER`` can use their own native, strict soft and soft reader/writer locks in sticky
directories even with an overflow UID or unreadable procfs. This includes OverlayFS containers. Use a private
directory if ownership cannot be verified.
