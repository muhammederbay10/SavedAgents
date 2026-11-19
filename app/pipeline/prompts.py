"""The wording the pipeline sends to models, in one place, with a version.

Every step takes its prompt text from one `PipelinePrompts` object, so the
wording lives here and not in the steps, and tests pass their own. The
`version` is stored in each step's `enrichment_run.output` and log line, so an
evaluation can name the wording it scored. Any change to the wording changes
the version.

Templates fill in the post's own text through named placeholders, such as
`{caption}`. A template must contain exactly its placeholders, written bare,
and no other braces; the post's text is only ever a value, never a template.
"""

import string
from dataclasses import dataclass, fields
from typing import Final

# The placeholders each template must contain, exactly. Fields not listed take none.
_PLACEHOLDERS: Final[dict[str, frozenset[str]]] = {
    "classify_input": frozenset({"caption", "collection"}),
    "extraction_input": frozenset({"caption", "collection", "transcript"}),
    "vocabulary_match_question": frozenset({"field"}),
    "vocabulary_input": frozenset({"field", "wording"}),
}


@dataclass(frozen=True)
class PipelinePrompts:
    """Everything the pipeline says to models.

    Attributes:
        version: Names this wording, e.g. `2026-11-18.1`.
        classify_type_question: Decision model: which built-in type, or none.
        classify_none_option: Decision model: the "none of these" option.
        classify_is_list_question: Decision model: does the post list several things?
        classify_input: The text next to the media; `{caption}` and `{collection}`.
        extraction_system: Text model: the rules of extraction.
        extraction_input: The post as the text model reads it; `{collection}`,
            `{caption}` and `{transcript}`.
        escalation_system: Video model: what to add to the rules of extraction
            when it watches the media itself.
        missing_value: What stands for a caption, collection or transcript the
            post does not have.
        privacy_gate_question: Decision model: is the wording generic, with
            nothing personal in it?
        vocabulary_match_question: Decision model: which existing value means
            the same as the wording, or new? `{field}` names the vocabulary field.
        vocabulary_new_option: Decision model: the "new" option.
        vocabulary_input: The wording as the decision model reads it;
            `{field}` and `{wording}`.
    """

    version: str
    classify_type_question: str
    classify_none_option: str
    classify_is_list_question: str
    classify_input: str
    extraction_system: str
    extraction_input: str
    escalation_system: str
    missing_value: str
    privacy_gate_question: str
    vocabulary_match_question: str
    vocabulary_new_option: str
    vocabulary_input: str

    def __post_init__(self) -> None:
        """Reject empty wording and templates without exactly their placeholders.

        Raises:
            ValueError: If a field is blank, has malformed braces, or does not
                contain exactly its placeholders as bare names.
        """
        for field in fields(self):
            text: str = getattr(self, field.name)
            if not text.strip():
                raise ValueError(f"prompt {field.name} must not be empty")
            expected = _PLACEHOLDERS.get(field.name, frozenset())
            if _placeholders(field.name, text) != expected:
                raise ValueError(
                    f"prompt {field.name} must contain exactly the placeholders {sorted(expected)}"
                )


def _placeholders(field_name: str, text: str) -> frozenset[str]:
    try:
        parsed = list(string.Formatter().parse(text))
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


# Approved by Muhammed on SAV-14 on 18 Nov 2026 (P1 and P2). The privacy-gate
# question is the one in docs/ARCHITECTURE.md §3.4, word for word. Change the
# version with any change to the wording, and score the change on the eval set.
PIPELINE_PROMPTS: Final = PipelinePrompts(
    version="2026-11-18.1",
    classify_type_question=(
        "What kind of saved post is this? Look at the video or image and read the caption. "
        "Pick the kind that fits, or 'none of these' if no kind fits well. "
        "The user's collection name is only a hint."
    ),
    classify_none_option="None of these kinds fits this post well.",
    classify_is_list_question=(
        "Does this post present several separate things of its kind (for example a list of "
        "places, movies or tips) rather than one?"
    ),
    classify_input="Caption: {caption}\nCollection: {collection}",
    extraction_system=(
        "You read one saved social-media post and fill in a JSON object that follows the "
        "schema. Use only what the collection name, caption and transcript say. Never invent "
        "names, places or facts. Copy names and titles exactly as the post gives them. Write "
        "the summary in the post's language. Write the category, tags and every other open "
        "value in English, lower case, in a few plain words. Use null or an empty list when "
        "the post doesn't say."
    ),
    extraction_input="Collection: {collection}\nCaption: {caption}\nTranscript: {transcript}",
    escalation_system=(
        "The caption and speech did not name it. Look at the media itself, including text on "
        "screen, signs, logos and recognisable scenes. Only name what you can actually see or "
        "hear. Leave the list empty if you can't identify anything."
    ),
    missing_value="(none)",
    privacy_gate_question=(
        "Is this a generic word or phrase that could describe anyone's save, with no name, "
        "handle, contact or address of a private person, and nothing that describes the user "
        "themselves ('my diagnosis', 'my ex', 'grandma Ayşe's recipe')?"
    ),
    vocabulary_match_question=(
        "A saved post's {field} is written as the wording below. Which existing value means "
        "the same thing? Pick 'new' if none of them does."
    ),
    vocabulary_new_option="None of these values means the same thing.",
    vocabulary_input="Field: {field}\nWording: {wording}",
)
