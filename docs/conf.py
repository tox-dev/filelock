from __future__ import annotations

from datetime import date, datetime

from filelock import __version__

name, company = "filelock", "tox-dev"
version, release = ".".join(__version__.split(".")[:2]), __version__
copyright = f"2014-{date.today().year}, {company}"
extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.autosectionlabel",
    "sphinx.ext.viewcode",
    "sphinx.ext.extlinks",
    "sphinx.ext.intersphinx",
    "sphinx_autodoc_typehints",
]
html_theme = "furo"
html_title, html_last_updated_fmt = name, datetime.now().isoformat()
pygments_style, pygments_dark_style = "sphinx", "monokai"
autoclass_content, autodoc_member_order, autodoc_typehints = "class", "bysource", "none"
autodoc_default_options = {"member-order": "bysource", "undoc-members": True, "show-inheritance": True}
autosectionlabel_prefix_document = True

intersphinx_mapping = {"python": ("https://docs.python.org/3", None)}
nitpicky = True
nitpick_ignore = [("py:class", "_thread._local")]
extlinks = {
    "issue": ("https://github.com/tox-dev/py-filelock/issues/%s", "issue #%s"),
    "pr": ("https://github.com/tox-dev/py-filelock/issues/%s", "PR #%s"),
    "user": ("https://github.com/%s", "@%s"),
}
