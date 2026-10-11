#!/usr/bin/env python3
"""The prompt sent with every image, its version and the reply schema.

Kept apart from describe.py so tools that only count descriptions (such as
progress.py) can read PROMPT_VER without importing Pillow.

This module uses only the standard library.
"""

# Raise this whenever DESCRIBE_PROMPT or DESCRIBE_SCHEMA changes in a way
# that makes older descriptions worth redoing.
PROMPT_VER = 4

DESCRIBE_PROMPT = """\
Look at this image and reply with a single JSON object and nothing else. Use exactly these keys:
  "description": two or three sentences describing the image.
  "subjects": a list of the main subjects or objects shown.
  "text": every piece of text visible in the image, transcribed exactly, or an empty string if there is no text.
  "tags": a list of short keywords useful for searching.
  "colors": a list of the most prominent colors.
  "aesthetic_score": a float from 0 to 1 rating overall aesthetic appeal (1 = very appealing).
  "sharpness": a float from 0 to 1 rating focus and sharpness (1 = razor sharp).
  "exposure_score": a float from 0 to 1 rating exposure quality (1 = ideal exposure).
  "watermark_score": a float from 0 to 1 rating freedom from watermarks or text overlays (1 = clean, 0 = heavily watermarked).
For every score above, higher is better. Reply with only the JSON object. Do not wrap it in Markdown code fences."""

SCORE_FIELDS = ("aesthetic_score", "sharpness", "exposure_score",
                "watermark_score")

# JSON schema matching describe.parse_description's expected shape. Used to
# constrain llama.cpp sampling to a valid reply.
DESCRIBE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "description": {"type": "string", "minLength": 1},
        "subjects": {"type": "array", "items": {"type": "string"}},
        "text": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "colors": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["description", "subjects", "text", "tags", "colors"],
}
for _field in SCORE_FIELDS:
    DESCRIBE_SCHEMA["properties"][_field] = {
        "type": "number", "minimum": 0, "maximum": 1,
    }
    DESCRIBE_SCHEMA["required"].append(_field)
