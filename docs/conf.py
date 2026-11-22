# ruff:ignore[implicit-namespace-package]  # Sphinx runs conf.py as a script, not as a package
"""Configuration for Sphinx."""

from __future__ import annotations

import os
from abc import ABCMeta
from contextlib import ContextDecorator
from datetime import UTC, datetime
from pathlib import Path
from threading import local
from typing import TYPE_CHECKING, Final, override

from docutils.nodes import Element, Text, reference
from sphinx.domains.python import PythonDomain

import filelock

if TYPE_CHECKING:
    from types import SimpleNamespace

    from sphinx.addnodes import pending_xref
    from sphinx.application import Sphinx
    from sphinx.builders import Builder
    from sphinx.environment import BuildEnvironment

name, company = "filelock", "tox-dev"
now = datetime.now(tz=UTC)
version, release = ".".join(filelock.__version__.split(".")[:2]), filelock.__version__
copyright: Final[str] = f"2014-{now.date().year}, {company}"  # ruff:ignore[builtin-variable-shadowing]  # Sphinx reads the module-level copyright name
extensions = [
    "sphinx_llm.txt",
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
    "sphinxcontrib.towncrier.ext",  # render unreleased news fragments as a draft "Unreleased" section
    "sphinxext.opengraph",
]
# raw output lets the browser render the diagrams, so they can follow furo's light/dark toggle. The extension picks the
# theme below from the page background and re-renders on every toggle; neutral pairs grays with the semantic tints that
# docs/_static/custom.css applies.
mermaid_output_format: Final[str] = "raw"
mermaid_light_theme: Final[str] = "neutral"

# sphinxcontrib-towncrier: the .. towncrier-draft-entries:: directive in changelog.rst shows the unreleased fragments.
towncrier_draft_autoversion_mode: Final[str] = "draft"
towncrier_draft_include_empty: Final[bool] = (
    True  # keep the docs build green right after a release, when no fragments remain
)
towncrier_draft_working_directory = Path(__file__).parent.parent

# sphinx-copybutton: add copy button to code blocks
copybutton_prompt_is_regexp: Final[bool] = True
copybutton_prompt_text: Final[str] = r">>> |\.\.\. |\$ |In \[\d*\]: | {2,5}\.\.\.: "

# sphinx-design: no special config needed

# Read the Docs serves pages under /<language>/<version>/; a bare domain base points the canonical link, og:url and
# sitemap at pages that 404.
html_baseurl = os.environ.get("READTHEDOCS_CANONICAL_URL", "https://py-filelock.readthedocs.io/en/stable/")
# sphinx-sitemap: generate sitemap.xml; html_baseurl already carries the language and version.
sitemap_url_scheme: Final[str] = "{link}"
# sphinx-notfound-page: custom 404 pages (no config needed)

# sphinxext-opengraph: social media metadata
ogp_site_url = html_baseurl
ogp_social_cards = {"enable": False}
ogp_use_first_image: Final[bool] = True
ogp_description_length: Final[int] = 200

html_theme: Final[str] = "furo"
html_logo: Final[str] = "logo.svg"
html_favicon: Final[str] = "logo.svg"
html_static_path = ["_static"]
html_css_files = ["custom.css"]
html_title, html_last_updated_fmt = name, now.isoformat()
pygments_style, pygments_dark_style = "sphinx", "monokai"
autoclass_content, autodoc_member_order, autodoc_typehints = "both", "bysource", "none"
# Dataclasses copy base field annotations into the subclass constructor, outside the module that imports these types.
autodoc_type_aliases = {
    "ExitStack": "contextlib.ExitStack",
    "FrameType": "types.FrameType",
    "Future": "concurrent.futures.Future",
    "InFlight": "filelock.InFlight",
    "LeaseClaim": "filelock.LeaseClaim",
    "StrictRelease": "filelock.StrictRelease",
}
# _typeshed exists only inside type checkers; sphinx-autodoc-typehints executes TYPE_CHECKING imports to resolve hints.
autodoc_mock_imports = ["_typeshed"]
autodoc_default_options = {"member-order": "bysource", "undoc-members": True, "show-inheritance": True}
autosectionlabel_prefix_document: Final[bool] = True
autosectionlabel_maxdepth: Final[int] = 2

intersphinx_mapping = {"python": ("https://docs.python.org/3", None)}
nitpicky: Final[bool] = True
# Towncrier news fragments are rST snippets folded into changelog.rst at release; they are not standalone pages.
exclude_patterns = ["changelog/*"]
extlinks = {
    "issue": ("https://github.com/tox-dev/py-filelock/issues/%s", "issue #%s"),
    "pr": ("https://github.com/tox-dev/py-filelock/issues/%s", "PR #%s"),
    "user": ("https://github.com/%s", "@%s"),
}


def setup(app: Sphinx) -> None:
    public_targets = {}
    for module in (filelock.asyncio, filelock):
        for export in module.__all__:
            cls = getattr(module, export)
            if isinstance(cls, type) and cls.__name__ == export:
                public_targets[f"{cls.__module__}.{cls.__qualname__}"] = f"{module.__name__}.{export}"

    class PatchedPythonDomain(PythonDomain):
        @override
        def resolve_xref(
            self,
            env: BuildEnvironment,
            fromdocname: str,
            builder: Builder,
            type: str,
            target: str,
            node: pending_xref,
            contnode: Element,
        ) -> reference | None:
            if target in public_targets:
                target = node["reftarget"] = public_targets[target]
            mapping = {
                "_thread._local": ("threading.local", "local"),
                "asyncio.events.AbstractEventLoop": ("asyncio.AbstractEventLoop", "AbstractEventLoop"),
                "concurrent.futures._base.Executor": ("concurrent.futures.Executor", "Executor"),
            }
            if target in mapping:
                of_type, with_name = mapping[target]
                target = of_type
                node["reftarget"] = of_type
                contnode.replace(contnode.children[0], Text(with_name))
            return super().resolve_xref(env, fromdocname, builder, type, target, node, contnode)

    app.add_domain(PatchedPythonDomain, override=True)
    app.connect("autodoc-process-bases", _public_bases)
    app.connect("autodoc-process-docstring", _public_override, priority=600)


def _public_bases(_app: Sphinx, _name: str, cls: type, _options: SimpleNamespace | None, bases: list[type]) -> None:
    exports = {*filelock.__all__, *filelock.asyncio.__all__}
    if any(base.__module__.startswith("filelock.") and base.__name__ not in exports for base in cls.__bases__):
        # Skip private mixins while retaining unrelated public bases, such as threading.local.
        public = []
        for base in cls.__mro__[1:]:
            if (base.__name__ in exports or base in {ABCMeta, ContextDecorator, local}) and not any(
                issubclass(child, base) for child in public
            ):
                public.append(base)
        bases[:] = public or [object]


def _public_override(
    _app: Sphinx,
    what: str,
    name: str,
    _obj: object,  # simplint: ignore[imprecise-annotation]  # autodoc visits every exported value
    _options: object,  # simplint: ignore[imprecise-annotation]  # Sphinx supplies its private directive options here
    lines: list[str],
) -> None:
    if what not in {"method", "property"} or not lines or not lines[-1].startswith(":Overrides:"):
        return
    cls_name, member = name.rsplit(".", 2)[-2:]
    cls = getattr(filelock, cls_name)
    if base := next(
        (base for base in cls.__mro__[1:] if base.__name__ in filelock.__all__ and hasattr(base, member)), None
    ):
        role = "attr" if what == "property" else "meth"
        target = f"{base.__name__}.{member}"
        lines[-1] = f":Overrides: :py:{role}:`{target} <filelock.{target}>`"
    else:
        lines.pop()


markdown_http_base = os.environ.get("READTHEDOCS_CANONICAL_URL", "")
