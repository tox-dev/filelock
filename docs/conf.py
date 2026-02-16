# noqa: INP001
"""Configuration for Sphinx."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from docutils.nodes import Element, Text, reference
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
    "sphinx_copybutton",
    "sphinx_design",
    "sphinx_sitemap",
    "sphinxcontrib.mermaid",
    "sphinxext.opengraph",
]
mermaid_output_format = "raw"

# sphinx-copybutton: add copy button to code blocks
copybutton_prompt_is_regexp = True
copybutton_prompt_text = r">>> |\.\.\. |\$ |In \[\d*\]: | {2,5}\.\.\.: "

# sphinx-design: no special config needed

# sphinx-sitemap: generate sitemap.xml
# sphinx-notfound-page: custom 404 pages (no config needed)

# sphinxext-opengraph: social media metadata
ogp_site_url = "https://py-filelock.readthedocs.io"
ogp_social_cards = {"enable": False}
ogp_use_first_image = True
ogp_description_length = 200

html_theme = "furo"
html_title, html_last_updated_fmt = name, now.isoformat()
html_baseurl = "https://py-filelock.readthedocs.io/"
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
        def resolve_xref(  # noqa: PLR0913, PLR0917
            self,
            env: BuildEnvironment,
            fromdocname: str,
            builder: Builder,
            type: str,  # noqa: A002
            target: str,
            node: pending_xref,
            contnode: Element,
        ) -> reference | None:
            mapping = {"_thread._local": ("threading.local", "local")}
            if target in mapping:
                of_type, with_name = mapping[target]
                target = of_type
                node["reftarget"] = of_type
                contnode.replace(contnode.children[0], Text(with_name))
            return super().resolve_xref(env, fromdocname, builder, type, target, node, contnode)

    app.add_domain(PatchedPythonDomain, override=True)
