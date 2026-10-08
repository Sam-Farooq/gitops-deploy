"""A deliberately tiny Go template evaluator, for one job.

argocd/applicationsets/services.yaml varies two fields per element, and the
only place an ApplicationSet can vary a field structurally is templatePatch,
which is a string. A string is not YAML until something renders it, and the
only renderer for it is Argo CD's own controller.

So the patch is rendered here instead, offline, and the result is merged onto
the template exactly as the controller would. That makes the eight Applications
real objects the rules can assert on, rather than text a regex is hopeful about.

What is supported: `{{ .field }}`, `{{- if .field }}`, `{{- else }}`,
`{{- end }}`, and the `-` trim markers. Everything else raises. A `range` or a
pipeline added to the patch is then an error here rather than a difference
between what this file says and what the cluster gets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_ACTION = re.compile(r"\{\{(-?)\s*(.*?)\s*(-?)\}\}", re.DOTALL)
_FIELD = re.compile(r"^\.([A-Za-z_][A-Za-z0-9_]*)$")
_IF = re.compile(r"^if\s+\.([A-Za-z_][A-Za-z0-9_]*)$")


class TemplateError(ValueError):
    """The template uses something this evaluator refuses to guess at."""


@dataclass
class _Text:
    text: str


@dataclass
class _Field:
    name: str


@dataclass
class _If:
    name: str
    then: list
    otherwise: list


def _tokenize(source: str) -> list:
    """Text and action tokens, with the trim markers already applied."""
    tokens: list = []
    position = 0
    pending_right_trim = False
    for match in _ACTION.finditer(source):
        text = source[position : match.start()]
        if pending_right_trim:
            text = text.lstrip()
        if match.group(1) == "-":
            text = text.rstrip()
        if text:
            tokens.append(_Text(text))
        tokens.append(match.group(2))
        pending_right_trim = match.group(3) == "-"
        position = match.end()
    tail = source[position:]
    if pending_right_trim:
        tail = tail.lstrip()
    if tail:
        tokens.append(_Text(tail))
    return tokens


def _parse(tokens: list, index: int = 0, depth: int = 0) -> tuple[list, int]:
    nodes: list = []
    while index < len(tokens):
        token = tokens[index]
        if isinstance(token, _Text):
            nodes.append(token)
            index += 1
            continue
        if token in ("end", "else"):
            if depth == 0:
                raise TemplateError(f"{{{{ {token} }}}} with no open if")
            return nodes, index
        if match := _IF.match(token):
            body, index = _parse(tokens, index + 1, depth + 1)
            otherwise: list = []
            if tokens[index] == "else":
                otherwise, index = _parse(tokens, index + 1, depth + 1)
            if tokens[index] != "end":
                raise TemplateError("if without end")
            nodes.append(_If(match.group(1), body, otherwise))
            index += 1
            continue
        if match := _FIELD.match(token):
            nodes.append(_Field(match.group(1)))
            index += 1
            continue
        raise TemplateError(f"unsupported action {{{{ {token} }}}}")
    if depth:
        raise TemplateError("if without end")
    return nodes, index


def _render(nodes: list, values: dict) -> str:
    out = []
    for node in nodes:
        if isinstance(node, _Text):
            out.append(node.text)
        elif isinstance(node, _Field):
            if node.name not in values:
                # Mirrors goTemplateOptions: ["missingkey=error"].
                raise TemplateError(f"no value for .{node.name}")
            out.append(str(values[node.name]))
        elif isinstance(node, _If):
            if node.name not in values:
                raise TemplateError(f"no value for .{node.name}")
            branch = node.then if values[node.name] else node.otherwise
            out.append(_render(branch, values))
    return "".join(out)


def render(source: str, values: dict) -> str:
    nodes, _ = _parse(_tokenize(source))
    return _render(nodes, values)


def merge(base: dict, patch: dict) -> dict:
    """A strategic merge of the kind the ApplicationSet controller applies.

    Maps merge key by key and anything else is replaced, which is what a JSON
    merge patch does. Lists are replaced rather than appended: an Application's
    syncOptions is a list, and a patch that appended to it would quietly end up
    with the same option twice.
    """
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = value
    return out
