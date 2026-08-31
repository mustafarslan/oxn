"""Shared ground for the oracle comparisons, and nothing tool-specific.

Split out of `test_oracles.py`, which had grown to eight oracle families behind four
module-level `importorskip` calls: a missing `complexipy` skipped the SCIP, grimp, PMD and
vulture tests too, none of which use it. Coverage vanishing behind an unrelated guard is
the failure this project keeps finding elsewhere, and it was sitting in the oracle suite.

Each family now lives in its own file with only its own skip conditions. What is genuinely
shared lives here.
"""

from __future__ import annotations

from pathlib import Path

from oxn.languages import get_parser
from oxn.metrics import cognitive_complexity
from oxn.profiles import get_profile

CORPUS = Path("benchmarks/corpora/python-httpx")


def _oxn_functions(path: Path) -> dict[str, int] | None:
    """Leaf name -> cognitive score for every function in a file.

    **Same-named functions collapse, and the last one in source order wins.** A file with
    four `__init__` methods reports one score. That is not ideal, but it is deliberate and
    must stay stable: complexipy keys its own output the same way, so both sides collapse
    identically and the comparison stays honest. Changing the traversal order silently
    changes *which* `__init__` is compared -- measured, 32 of httpx's 962 functions --
    without changing the count, so a refactor here looks free and is not.
    """
    profile = get_profile("python")
    root = get_parser("python").parse(path.read_bytes()).root_node
    if root.has_error:
        return None
    return {
        name: cognitive_complexity(node, profile, function_name=name).score
        for name, node in _named_functions(root, profile)
    }


def _function_nodes(root, profile):
    """Every function-like definition in the tree, in source order, classes descended into.

    Yields the node whether or not it is named -- a lambda is a function for the purposes
    of comparing against a tool that reports one. A definition's *body* is what gets
    descended into, not the definition node: entering the node again would re-find the
    definition itself and never terminate.

    Recursive, and deliberately so: definition nesting is bounded by how deeply a person
    will nest a class in a function, which is nothing like the depth of an expression tree.
    Source order is load-bearing for `_oxn_functions` below -- see the note there.
    """
    for child in root.named_children:
        definition = profile.unwrap(child)
        if definition.type in profile.function_like:
            yield definition
        if definition.type in (profile.function_like | profile.class_like):
            body = definition.child_by_field_name(profile.body_field)
            yield from _function_nodes(body if body is not None else child, profile)
        else:
            yield from _function_nodes(child, profile)


def _named_functions(root, profile):
    """The subset of `_function_nodes` that carries a name, paired with it."""
    for definition in _function_nodes(root, profile):
        name = profile.entity_name(definition)
        if name:
            yield name, definition


def _count_kinds(node, profile, kinds: set[str]) -> int:
    """Occurrences of ``kinds`` inside one function, excluding nested definitions."""
    total = 0
    stack = list(node.named_children)
    while stack:
        current = profile.unwrap(stack.pop())
        if profile.is_definition(current):
            continue
        if current.type in kinds:
            total += 1
        stack.extend(current.named_children)
    return total
