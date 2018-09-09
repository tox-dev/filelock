#!/usr/bin/env python

# This is free and unencumbered software released into the public domain.
#
# Anyone is free to copy, modify, publish, use, compile, sell, or
# distribute this software, either in source code form or as a compiled
# binary, for any purpose, commercial or non-commercial, and by any
# means.
#
# In jurisdictions that recognize copyright laws, the author or authors
# of this software dedicate any and all copyright interest in the
# software to the public domain. We make this dedication for the benefit
# of the public at large and to the detriment of our heirs and
# successors. We intend this dedication to be an overt act of
# relinquishment in perpetuity of all present and future rights to this
# software under copyright law.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# IN NO EVENT SHALL THE AUTHORS BE LIABLE FOR ANY CLAIM, DAMAGES OR
# OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
# ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.
#
# For more information, please refer to <http://unlicense.org>


# Modules
# ------------------------------------------------
from setuptools import setup
from filelock import __version__

# Main
# ------------------------------------------------
try:
    long_description = open("README.rst").read()
except OSError:
    long_description = "not available"

try:
    license_ = open("LICENSE.rst").read()
except OSError:
    license_ = "not available"

setup(
    name = "filelock",
    version = __version__,
    description = "A platform independent file lock.",
    long_description = long_description,
    author = "Benedikt Schmitt",
    author_email = "benedikt@benediktschmitt.de",
    url = "https://github.com/benediktschmitt/py-filelock",
    download_url = "https://github.com/benediktschmitt/py-filelock/archive/master.zip",
    py_modules = ["filelock"],
    license = license_,
    classifiers = [
        "License :: Public Domain",
        "Development Status :: 5 - Production/Stable",
        "Operating System :: OS Independent",
        "Programming Language :: Python",
        "Intended Audience :: Developers",
        "Topic :: System",
        "Topic :: Internet",
        "Topic :: Software Development :: Libraries"
        ],
    )
