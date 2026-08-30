"""Import extraction and specifier resolution."""

from __future__ import annotations

import pytest

from oxn.graph.imports import extract_imports
from oxn.graph.resolve import ResolutionContext, resolve_import
from oxn.languages import get_parser
from oxn.profiles import get_profile


def imports(language: str, source: str):
    profile = get_profile(language)
    root = get_parser(language).parse(source.encode()).root_node
    return extract_imports(root, profile)


def specs(language: str, source: str) -> list[str]:
    return [item.specifier for item in imports(language, source)]


# ---- extraction --------------------------------------------------------------------------


def test_python_absolute_and_aliased() -> None:
    assert specs("python", "import os\nimport os.path as osp\nimport a.b.c\n") == [
        "os",
        "os.path",
        "a.b.c",
    ]


def test_python_from_imports_record_their_names() -> None:
    found = imports("python", "from a.b import c, d as e\n")[0]
    assert found.specifier == "a.b"
    # The *real* name matters for resolution; the alias does not.
    assert found.names == ("c", "d")


def test_python_relative_levels() -> None:
    found = imports("python", "from . import x\nfrom .rel import y\nfrom ..up import z\n")
    assert [(item.level, item.specifier) for item in found] == [
        (1, ""),
        (1, "rel"),
        (2, "up"),
    ]


def test_wildcard_imports_are_marked() -> None:
    assert imports("python", "from a import *\n")[0].kind == "wildcard"


def test_imports_hidden_inside_blocks_are_found() -> None:
    """A scan of top-level statements misses these, and under-reported coupling is silent."""
    source = (
        "if TYPE_CHECKING:\n    from typing import Any\n"
        "def f():\n    import json\n    return json\n"
        "try:\n    import fast\nexcept ImportError:\n    import slow\n"
    )
    assert set(specs("python", source)) == {"typing", "json", "fast", "slow"}


def test_python_dynamic_imports_with_a_literal() -> None:
    found = imports("python", 'mod = importlib.import_module("dyn.mod")\n')[0]
    assert (found.kind, found.specifier) == ("dynamic", "dyn.mod")


def test_typescript_import_shapes() -> None:
    source = (
        'import a from "./mod";\n'
        'import { b, c as d } from "../other";\n'
        'import * as ns from "pkg";\n'
        'export { x } from "./re-export";\n'
    )
    assert specs("typescript", source) == ["./mod", "../other", "pkg", "./re-export"]


def test_type_only_imports_are_distinguished() -> None:
    """`import type` is erased at compile time, so it is not runtime coupling."""
    found = imports("typescript", 'import type { T } from "./types";\n')[0]
    assert found.kind == "type_only"


def test_side_effect_imports_are_distinguished() -> None:
    assert imports("typescript", 'import "./polyfill";\n')[0].kind == "side_effect"


def test_export_without_a_source_is_not_an_import() -> None:
    assert specs("typescript", "export const x = 1;\n") == []


def test_ecmascript_dynamic_imports() -> None:
    found = imports(
        "javascript", 'const a = await import("./lazy");\nconst b = require("legacy");\n'
    )
    assert {(item.kind, item.specifier) for item in found} == {
        ("dynamic", "./lazy"),
        ("dynamic", "legacy"),
    }


# ---- resolution --------------------------------------------------------------------------


@pytest.fixture
def python_tree() -> ResolutionContext:
    files = {
        "src/pkg/__init__.py",
        "src/pkg/core.py",
        "src/pkg/sub/__init__.py",
        "src/pkg/sub/deep.py",
        "top.py",
    }
    from pathlib import Path

    return ResolutionContext.build(Path("."), files)


def resolve(source: str, language: str, context: ResolutionContext, code: str):
    """Primary target per import, for the single-target cases."""
    return [
        resolve_import(source, raw, context, language).target for raw in imports(language, code)
    ]


def resolve_all(source: str, language: str, context: ResolutionContext, code: str):
    """Every file an import reaches."""
    return [
        list(resolve_import(source, raw, context, language).targets)
        for raw in imports(language, code)
    ]


def test_python_absolute_import_resolves_through_the_package_root(python_tree) -> None:
    assert resolve("src/pkg/core.py", "python", python_tree, "import pkg.sub.deep\n") == [
        "src/pkg/sub/deep.py"
    ]


def test_python_package_import_resolves_to_init(python_tree) -> None:
    assert resolve("top.py", "python", python_tree, "import pkg\n") == ["src/pkg/__init__.py"]


def test_from_package_import_module_reaches_both(python_tree) -> None:
    """`from pkg import core` imports the package *and* the submodule.

    Python executes both, and grimp reports both edges. Modelling this as a single target
    silently loses an edge whenever the package is not imported anywhere else.
    """
    assert resolve_all("top.py", "python", python_tree, "from pkg import core\n") == [
        ["src/pkg/__init__.py", "src/pkg/core.py"]
    ]


def test_from_package_import_symbol_reaches_only_the_package(python_tree) -> None:
    assert resolve_all("top.py", "python", python_tree, "from pkg import CONST\n") == [
        ["src/pkg/__init__.py"]
    ]


def test_from_module_import_symbol_reaches_only_the_module(python_tree) -> None:
    assert resolve_all("top.py", "python", python_tree, "from pkg.core import thing\n") == [
        ["src/pkg/core.py"]
    ]


def test_python_relative_imports_anchor_to_the_importing_package(python_tree) -> None:
    assert resolve("src/pkg/sub/deep.py", "python", python_tree, "from . import x\n") == [
        "src/pkg/sub/__init__.py"
    ]
    assert resolve("src/pkg/sub/deep.py", "python", python_tree, "from ..core import y\n") == [
        "src/pkg/core.py"
    ]


def test_third_party_imports_are_external(python_tree) -> None:
    assert resolve("top.py", "python", python_tree, "import os\nimport httpx\n") == [None, None]


def test_from_package_import_subpackage_reaches_both(python_tree) -> None:
    assert resolve_all("src/pkg/core.py", "python", python_tree, "from pkg import sub\n") == [
        ["src/pkg/__init__.py", "src/pkg/sub/__init__.py"]
    ]


@pytest.fixture
def ts_tree() -> ResolutionContext:
    from pathlib import Path

    files = {
        "packages/core/index.ts",
        "packages/core/injector.ts",
        "packages/common/index.ts",
        "app/main.ts",
    }
    context = ResolutionContext.build(Path("."), files)
    context.ts_aliases = {
        "@nestjs/core": ("packages/core",),
        "@nestjs/common": ("packages/common",),
    }
    return context


def test_typescript_relative_import(ts_tree) -> None:
    assert resolve(
        "app/main.ts", "typescript", ts_tree, 'import a from "../packages/core/injector";\n'
    ) == ["packages/core/injector.ts"]


def test_typescript_js_specifier_resolves_to_the_ts_file(ts_tree) -> None:
    """TypeScript source imports the *emitted* name: `./x.js` means `./x.ts`.

    Missing this rule loses essentially every relative import in a modern TS codebase --
    402 of nest's 410, measured.
    """
    assert resolve(
        "app/main.ts", "typescript", ts_tree, 'import a from "../packages/core/injector.js";\n'
    ) == ["packages/core/injector.ts"]


def test_typescript_directory_import_resolves_to_index(ts_tree) -> None:
    assert resolve("app/main.ts", "typescript", ts_tree, 'import a from "../packages/core";\n') == [
        "packages/core/index.ts"
    ]


def test_tsconfig_path_aliases_resolve(ts_tree) -> None:
    assert resolve("app/main.ts", "typescript", ts_tree, 'import a from "@nestjs/core";\n') == [
        "packages/core/index.ts"
    ]
    assert resolve(
        "app/main.ts", "typescript", ts_tree, 'import a from "@nestjs/core/injector.js";\n'
    ) == ["packages/core/injector.ts"]


def test_unknown_package_is_external(ts_tree) -> None:
    assert resolve("app/main.ts", "typescript", ts_tree, 'import a from "rxjs";\n') == [None]


# ---- tsconfig is JSONC, not JSON -----------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        (
            "trailing line comment after a value",
            '{\n "compilerOptions": {\n  "baseUrl": "./src", // where sources live\n'
            '  "paths": {"@a/*": ["a/*"]}\n }\n}',
        ),
        ("full-line comment", '{\n // note\n "compilerOptions": {"baseUrl": "."}\n}'),
        ("block comment", '{\n /* a note\n spanning lines */\n "compilerOptions": {}\n}'),
        ("trailing comma in an object", '{"compilerOptions":{"baseUrl":".",}}'),
        ("trailing comma in an array", '{"compilerOptions":{"paths":{"@a/*":["a/*",]}}}'),
        ("comment marker inside a string", '{"compilerOptions":{"baseUrl":"a//b"}}'),
        (
            "escaped quote before a comment marker",
            r'{"compilerOptions":{"baseUrl":"say \"hi\" // not a comment"}}',
        ),
    ],
)
def test_jsonc_shapes_that_appear_in_real_tsconfigs(name: str, raw: str) -> None:
    """A tsconfig is JSONC: comments and trailing commas are legal in it.

    A line-level heuristic gets this wrong in both directions -- it strips a URL inside a
    string, and it fails to strip a comment that follows a value on the same line. Either
    way every path alias in the file is silently lost.
    """
    import json

    from oxn.graph.resolve import _strip_jsonc

    json.loads(_strip_jsonc(raw))  # must not raise


def test_a_url_inside_a_string_is_not_treated_as_a_comment() -> None:
    import json

    from oxn.graph.resolve import _strip_jsonc

    raw = '{"compilerOptions":{"paths":{"@x/*":["http://example.com/*"]}}}'
    parsed = json.loads(_strip_jsonc(raw))
    assert parsed["compilerOptions"]["paths"]["@x/*"] == ["http://example.com/*"]


def test_tsconfig_aliases_survive_comments(tmp_path) -> None:
    """End to end: a commented tsconfig must still yield working path aliases."""
    from oxn.graph.resolve import ResolutionContext

    (tmp_path / "tsconfig.json").write_text(
        "{\n"
        "  // paths for the monorepo\n"
        '  "compilerOptions": {\n'
        '    "baseUrl": ".", // repo root\n'
        '    "paths": {"@core/*": ["packages/core/*"],}\n'
        "  }\n"
        "}\n"
    )
    context = ResolutionContext.build(tmp_path, {"packages/core/index.ts"})
    assert context.ts_aliases == {"@core": ("packages/core",)}
