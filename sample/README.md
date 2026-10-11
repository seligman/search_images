# Demo Sample Images

The `images/` folder holds 18 freely licensed images for trying out the
project. See `images/CREDITS.md` for licenses and attribution.

Two images contain text for testing the LLM's OCR ability:

- `17-stop-sign.jpg` - a stop sign
- `18-chalkboard-menu.jpg` - a deli menu board

## Running the demo

A library cannot be inside the program folder, so first copy the pictures
to a folder of their own (any folder outside the program folder works).
From the program folder, on Linux or macOS:

    cp -r sample/images ~/Pictures/search-demo

or on Windows:

    xcopy /e /i sample\images C:\Photos\Demo

1. Start the server and open the address it prints:

       python serve.py

2. On the **Settings** page, add an endpoint and press **Test** (see
   "Describing pictures" in the main [README](../README.md)).
3. On the **Manage** page, press **Add library**, enter the folder you
   copied the pictures to and the name "Demo", and leave **Scan and
   describe it right away** ticked.
4. When the job panel shows the job is done, open **Search**.

Or from the command line: run `python settings.py` (add the endpoint under
"Configure LLM endpoints" and the library under "Manage image libraries",
then "Save and exit"), then `python scan.py`, then `python serve.py`.

Try searches like "cat", "snow", "yellow flower", "stop", or words from the
deli menu once the images have been described.

When you are done, remove the library on the Manage page (deleting its
indexed data) and delete the copied folder.
