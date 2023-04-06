from __future__ import annotations

from datetime import date, datetime

from docutils.nodes import Element, Text
from sphinx.addnodes import pending_xref
from sphinx.application import Sphinx
from sphinx.builders import Builder
from sphinx.domains.python import PythonDomain
from sphinx.environment import BuildEnvironment

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
nitpick_ignore = []
extlinks = {
    "issue": ("https://github.com/tox-dev/py-filelock/issues/%s", "issue #%s"),
    "pr": ("https://github.com/tox-dev/py-filelock/issues/%s", "PR #%s"),
    "user": ("https://github.com/%s", "@%s"),
}


def setup(app: Sphinx) -> None:
    class PatchedPythonDomain(PythonDomain):
        def resolve_xref(
            self,
            env: BuildEnvironment,
            fromdocname: str,
            builder: Builder,
            type: str,
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
