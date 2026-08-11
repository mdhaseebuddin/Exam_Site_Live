"""
Marshmallow input-validation schemas for the Exam Platform.

These schemas act as a strict, transport-level validation layer that runs
BEFORE any request payload reaches the database. They reject:

  * malformed JSON / wrong types
  * unexpected (unknown) fields — via `unknown=RAISE`
  * out-of-range values

This keeps the underlying database logic untouched; we merely lock the door
on the way in.
"""

from marshmallow import Schema, fields, validates_schema, ValidationError


class StudentSchema(Schema):
    """Subset of the student dict included in a /submit payload.

    The host can define ANY number of custom registration fields (address,
    department, ...), whose slugs appear as top-level keys in the student
    dict. We therefore keep the known fields typed but INCLUDE (rather than
    reject) the dynamic custom slugs. Strictness is enforced at the outer
    SubmitPayloadSchema level.
    """

    name = fields.String(allow_none=True)
    phone = fields.String(allow_none=True)
    registered_at = fields.String(allow_none=True)

    # Marshmallow 4.x: keep arbitrary custom registration fields (dynamic
    # slugs) instead of raising on them.
    unknown = "INCLUDE"


class SubmitPayloadSchema(Schema):
    """
    The top-level request body for POST /exam/<id>/submit.

    Expected shape:
        {
          "student": { "name": ..., "phone": ..., ... },
          "answers": { "0": <int|str>, "1": <int|str>, ... }
        }

    We reject any unexpected top-level key (`unknown=RAISE`) and enforce
    that `answers` is a dict of string keys (question indices) whose values
    are either ints (MCQ selection) or strings (essay / coding).
    """

    # unknown="INCLUDE" (not the outer RAISE) so the dynamic custom
    # registration slugs (address, department, ...) pass through.
    student = fields.Nested(StudentSchema, allow_none=True, unknown="INCLUDE")
    answers = fields.Dict(
        keys=fields.String(),
        values=fields.Raw(),
        allow_none=True,
        load_default=dict,
    )

    # Marshmallow 4.x: reject unknown fields on load.
    unknown = "RAISE"

    @validates_schema
    def _check_answer_types(self, data, **kwargs):
        """Reject answer values that are neither int nor str.

        This prevents a caller from smuggling in arbitrary objects (lists,
        dicts, booleans) as answers, which could otherwise be stored in the
        JSON `response` column or break the grading logic.
        """
        answers = data.get("answers") or {}
        for key, value in answers.items():
            if value is not None and not isinstance(value, (int, str)):
                raise ValidationError(
                    f"Answer for question '{key}' must be an integer or a string.",
                    field_name="answers",
                )


class QuestionSchema(Schema):
    """Validation for the host "add question" form (POST /host/question)."""

    text = fields.String(required=True, validate=lambda v: v.strip() != "")
    type = fields.String(validate=lambda v: v in ("mcq", "essay", "coding"))
    options = fields.List(fields.String(), required=False, allow_none=True)
    correct_index = fields.Integer(required=False, allow_none=True)

    # Marshmallow 4.x: reject unknown fields on load.
    unknown = "RAISE"


def validate_submit_payload(payload: dict) -> str | None:
    """
    Validate a /submit request body. Returns an error message string on
    failure, otherwise None.

    Usage in the route:
        err = validate_submit_payload(payload)
        if err:
            return jsonify({"error": err}), 400
    """
    try:
        SubmitPayloadSchema().load(payload or {})
    except ValidationError as exc:
        # Flatten nested messages into a readable single-line error.
        msgs = exc.messages
        if isinstance(msgs, dict):
            parts = []
            for key, val in msgs.items():
                if isinstance(val, list):
                    parts.append(f"{key}: {'; '.join(str(v) for v in val)}")
                else:
                    parts.append(f"{key}: {val}")
            return "Invalid payload: " + "; ".join(parts)
        return "Invalid payload: " + str(msgs)
    return None


def validate_question_payload(data: dict) -> str | None:
    """
    Validate the host "add question" form data. Returns an error message
    string on failure, otherwise None.

    Note: `unknown=RAISE` is intentionally NOT applied here because the
    HTML form posts many helper fields (options are multi-valued). We only
    enforce the fields we actually consume.
    """
    # Build a clean dict from the raw form data.
    clean = {
        "text": data.get("text", ""),
        "type": data.get("type", "mcq"),
        "options": data.get("options") or None,
    }
    try:
        ci = data.get("correct_index")
        clean["correct_index"] = int(ci) if ci is not None else None
    except (TypeError, ValueError):
        clean["correct_index"] = None

    try:
        QuestionSchema().load(clean)
    except ValidationError as exc:
        msgs = exc.messages
        if isinstance(msgs, dict):
            parts = []
            for key, val in msgs.items():
                if isinstance(val, list):
                    parts.append(f"{key}: {'; '.join(str(v) for v in val)}")
                else:
                    parts.append(f"{key}: {val}")
            return "Invalid question: " + "; ".join(parts)
        return "Invalid question: " + str(msgs)
    return None
