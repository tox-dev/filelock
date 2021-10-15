from __future__ import annotations

from datetime import date, datetime

from filelock import __version__

company = "tox-dev"
name = "filelock"
version = ".".join(__version__.split(".")[:2])
release = __version__
copyright = f"2014-{date.today().year}, {company}"

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.autosectionlabel",
    "sphinx.ext.extlinks",
    "sphinx.ext.intersphinx",
    "sphinx_autodoc_typehints",
]

templates_path = []
unused_docs = []
source_suffix = ".rst"
exclude_patterns = ["_build"]

master_doc = "index"
pygments_style = "default"

project = name
today_fmt = "%B %d, %Y"

html_theme = "furo"
html_favicon = "logo.svg"
html_logo = "logo.svg"
html_theme_options = {
    "navigation_with_keys": True,
}
html_title = name
html_last_updated_fmt = datetime.now().isoformat()

autoclass_content = "class"
autodoc_member_order = "bysource"
autodoc_default_options = {
    "member-order": "bysource",
    "undoc-members": True,
    "show-inheritance": True,
}
autodoc_typehints = "none"
always_document_param_types = False
typehints_fully_qualified = True
autosectionlabel_prefix_document = True

intersphinx_mapping = {"python": ("https://docs.python.org/3", None)}
nitpicky = True
nitpick_ignore = []
extlinks = {
    "issue": ("https://github.com/tox-dev/py-filelock/issues/%s", "issue #%s"),
    "pr": ("https://github.com/tox-dev/py-filelock/issues/%s", "PR #%s"),
    "user": ("https://github.com/%s", "@%s"),
}
