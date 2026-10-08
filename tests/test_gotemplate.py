"""The templatePatch evaluator. If this is wrong, every Argo CD test is wrong."""

import pytest

from gitopsvalidate.gotemplate import TemplateError, merge, render


def test_field_interpolation():
    assert render("name: {{ .service }}", {"service": "rag-agent"}) == "name: rag-agent"


def test_missing_key_is_an_error():
    # Mirrors goTemplateOptions: ["missingkey=error"] on the ApplicationSet. The
    # Go default renders an empty string, so a typo becomes a valid Application
    # pointing at nothing.
    with pytest.raises(TemplateError, match="no value for .servce"):
        render("{{ .servce }}", {"service": "rag-agent"})


def test_if_true_branch():
    out = render("a\n{{- if .on }}\nb\n{{- end }}\nc", {"on": True})
    assert out == "a\nb\nc"


def test_if_false_branch_leaves_nothing_behind():
    out = render("a\n{{- if .on }}\nb\n{{- end }}\nc", {"on": False})
    assert out == "a\nc"


def test_else():
    source = "{{- if .on }}yes{{- else }}no{{- end }}"
    assert render(source, {"on": True}) == "yes"
    assert render(source, {"on": False}) == "no"


def test_right_trim_removes_following_whitespace():
    assert render("a {{ .x -}}   b", {"x": "1"}) == "a 1b"


def test_nested_ifs():
    source = "{{- if .a }}A{{- if .b }}B{{- end }}{{- end }}"
    assert render(source, {"a": True, "b": True}) == "AB"
    assert render(source, {"a": True, "b": False}) == "A"


@pytest.mark.parametrize(
    "source",
    [
        "{{ range .items }}x{{ end }}",
        "{{ .a | upper }}",
        "{{ if eq .a 1 }}x{{ end }}",
        '{{ printf "%s" .a }}',
    ],
)
def test_unsupported_actions_raise(source):
    # Refusing is the point. A pipeline added to templatePatch has to break
    # this evaluator, not be silently approximated, or the checks would be
    # asserting on an Application the controller never produces.
    with pytest.raises(TemplateError):
        render(source, {"a": 1, "items": [1]})


def test_unbalanced_if_raises():
    with pytest.raises(TemplateError, match="if without end"):
        render("{{- if .a }}x", {"a": True})


def test_end_without_if_raises():
    with pytest.raises(TemplateError, match="no open if"):
        render("x{{- end }}", {})


def test_merge_is_recursive_on_maps():
    base = {"spec": {"a": 1, "nested": {"x": 1}}}
    patch = {"spec": {"b": 2, "nested": {"y": 2}}}
    assert merge(base, patch) == {"spec": {"a": 1, "b": 2, "nested": {"x": 1, "y": 2}}}


def test_merge_replaces_lists_rather_than_appending():
    # syncOptions is a list. Appending would leave the same option twice, and
    # Argo CD takes the last one, which makes the behaviour depend on order.
    base = {"syncOptions": ["a", "b"]}
    assert merge(base, {"syncOptions": ["c"]}) == {"syncOptions": ["c"]}
