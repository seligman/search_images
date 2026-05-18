#!/usr/bin/env python3
"""Example helper module for the image database.

Point settings.py at this file (or your own copy) to wrap the LLM calls.
Every function is optional; remove any you do not need.
"""


def before_launch():
    """Run once before the first LLM call of a scan pass.

    Only called when there is at least one image to describe. Use it to start
    a local model server, open a connection, and so on.
    """
    pass


def after_launch():
    """Run once after the last LLM call of a scan pass."""
    pass


def call_api(prompt, image_path):
    """Adjust a request just before it is sent to the LLM.

    Return a (prompt, image_path) pair to change what gets sent, or return
    None to leave the request unchanged.
    """
    return None
