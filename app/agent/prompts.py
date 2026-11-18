"""The wording search sends to the decision model, in one place, with a version.

Filter normalization takes its wording from one `RetrievalPrompts` object, so
the wording lives here and not in the search code, and tests pass their own.
The `version` is written in the normalization's log line, so an evaluation can
name the wording it scored. Any change to the wording changes the version.

The input template fills in the user's wording through named placeholders. A
template must contain exactly its placeholders, written bare, and no other
braces; the user's text is only ever a value, never a template.
"""

import string
from dataclasses import dataclass, fields
from typing import Final

# The placeholders each template must contain, exactly. Fields not listed take none.
_PLACEHOLDERS: Final[dict[str, frozenset[str]]] = {
    "normalization_question": frozenset({"filter"}),
    "normalization_input": frozenset({"filter", "wording"}),
}


@dataclass(frozen=True)
class RetrievalPrompts:
    """Everything search says to the decision model.

    Attributes:
        version: Names this wording, e.g. `2026-11-18.1`.
        normalization_question: Which of the user's saved values is the one
            they mean? `{filter}` names the filter, e.g. `area` or `streaming`.
        normalization_no_match_option: The option for "none of these values".
        normalization_input: What the user wrote, as the decision model reads
            it; `{filter}` and `{wording}`.
    """

    version: str
    normalization_question: str
    normalization_no_match_option: str
    normalization_input: str

    def __post_init__(self) -> None:
        """Reject empty wording and templates without exactly their placeholders.

        Raises:
            ValueError: If a field is blank, has malformed braces, or does not
                contain exactly its placeholders as bare names.
        """
        for field in fields(self):
            wording: str = getattr(self, field.name)
            if not wording.strip():
                raise ValueError(f"prompt {field.name} must not be empty")
            expected = _PLACEHOLDERS.get(field.name, frozenset())
            if _placeholders(field.name, wording) != expected:
                raise ValueError(
                    f"prompt {field.name} must contain exactly the placeholders {sorted(expected)}"
                )


def _placeholders(field_name: str, template: str) -> frozenset[str]:
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as error:
        raise ValueError(f"prompt {field_name} has malformed braces") from error
    names: set[str] = set()
    for _, name, format_spec, conversion in parsed:
        if name is None:
            continue
        if not name or format_spec or conversion is not None or not name.isidentifier():
            raise ValueError(f"prompt {field_name} may only use bare named placeholders")
        names.add(name)
    return frozenset(names)
