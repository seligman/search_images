# Demo Sample Images

The `images/` folder holds 19 freely licensed images for trying out the
project. See `images/CREDITS.md` for licenses and attribution.

Three images contain text for testing the LLM's OCR ability:

- `17-stop-sign.jpg` - a stop sign
- `18-chalkboard-menu.jpg` - a deli menu board
- `19-roys-motel-360.jpg` - the Roy's Motel and Cafe sign on Route 66

`19-roys-motel-360.jpg` is also a 360-degree panorama: the search page
opens it in a viewer you drag to look all around. Its sign is a good test
of reading text in a 360-degree photo, where the sign is only a small part
of a wide, stretched picture: words like "Roy's", "motel", "cafe",
"vacancy" and "route 66" should show up in the text read from it.

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
deli menu once the images have been described. For the panorama, try
"vacancy", "route 66" or "motel", or `is:360` for every 360-degree photo
and `has:text` for every picture with text in it.

Every picture has a date taken, so the timeline above the results shows
the months of 2026 they fall in: click April for the pictures from the
days around the panorama, or search `on:04-05` for the panorama's day.
The dates of pictures 01 to 18 were made up for the demo (see
`images/CREDITS.md`).

When you are done, remove the library on the Manage page (deleting its
indexed data) and delete the copied folder.
