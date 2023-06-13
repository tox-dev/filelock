# noqa: INP001
"""Configuration for Sphinx."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from docutils.nodes import Element, Text
from sphinx.domains.python import PythonDomain

from filelock import __version__

if TYPE_CHECKING:
    from sphinx.addnodes import pending_xref
    from sphinx.application import Sphinx
    from sphinx.builders import Builder
    from sphinx.environment import BuildEnvironment

name, company = "filelock", "tox-dev"
now = datetime.now(tz=timezone.utc)
version, release = ".".join(__version__.split(".")[:2]), __version__
copyright = f"2014-{now.date().year}, {company}"  # noqa: A001
extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.autosectionlabel",
    "sphinx.ext.viewcode",
    "sphinx.ext.extlinks",
    "sphinx.ext.intersphinx",
    "sphinx_autodoc_typehints",
]
html_theme = "furo"
html_title, html_last_updated_fmt = name, now.isoformat()
pygments_style, pygments_dark_style = "sphinx", "monokai"
autoclass_content, autodoc_member_order, autodoc_typehints = "class", "bysource", "none"
autodoc_default_options = {"member-order": "bysource", "undoc-members": True, "show-inheritance": True}
autosectionlabel_prefix_document = True

intersphinx_mapping = {"python": ("https://docs.python.org/3", None)}
nitpicky = True
nitpick_ignore = []
extlinks = {
    "issue": ("https://github.com/tox-dev/py-filelock/issues/%s", "issue #%s"),
    "pr": ("https://github.com/tox-dev/py-filelock/issues/%s", "PR #%s"),
    "user": ("https://github.com/%s", "@%s"),
}


def setup(app: Sphinx) -> None:
    """
    Setup app.

    :param app: the app
    """

    class PatchedPythonDomain(PythonDomain):
        def resolve_xref(  # noqa: PLR0913
            self,
            env: BuildEnvironment,
            fromdocname: str,
            builder: Builder,
            type: str,  # noqa: A002
            target: str,
            node: pending_xref,
            contnode: Element,
        ) -> Element:
            mapping = {"_thread._local": ("threading.local", "local")}
            if target in mapping:
                of_type, with_name = mapping[target]
                target = node["reftarget"] = of_type
                contnode.children[0] = Text(with_name, with_name)
            return super().resolve_xref(env, fromdocname, builder, type, target, node, contnode)

    app.add_domain(PatchedPythonDomain, override=True)
