# Demo Sample Images

The `images/` folder holds 18 freely licensed images for trying out the
project. See `images/CREDITS.md` for licenses and attribution.

Two images contain text for testing the LLM's OCR ability:

- `17-stop-sign.jpg` - a stop sign
- `18-chalkboard-menu.jpg` - a deli menu board

## Running the demo

1. Configure an LLM endpoint and add a library:

       python settings.py

   Add a library named "Demo" pointing at this folder's `images/` path.

2. Scan and describe the images:

       python scan.py

3. Search them:

       python serve.py

Try searches like "cat", "snow", "yellow flower", "stop", or words from the
deli menu once the images have been described.
