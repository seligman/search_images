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

## Places and the map

Ten of the pictures say where they were taken, as their source pages do
(see "Locations" in `images/CREDITS.md`). Six have a GPS position: the
photographer's geotag for five of them (03, 09, 11, 13 and 18) and the
camera's own for the panorama (19); 11 also names its place ("Cimadera,
Ticino, Switzerland", as its page does). Four have only the names of
their place, with no position, as their pages name only a town or a
continent: 02 ("Cottonwood, Idaho, United States"), 04 ("Alnmouth,
England, United Kingdom"), 05 ("Olinda, Victoria, Australia") and 16
("Antarctica").

- **Map** (in the page header) shows the six positions as pins on a
  world map; click a pin for its pictures. Pictures with only the names
  of their place have no pin: a pin at a town's centre would look more
  exact than their pages are. For a sharper world image run
  `python make_tiles.py` once, and **Street maps** on the Manage page
  downloads street maps around the pins (see the main README).
- The viewer's **Place** line shows the names of 02, 04, 05, 11 and 16,
  and searches like `idaho`, `alnmouth`, `olinda`, `antarctica`,
  `switzerland` or `place:australia` find them. For the other pictures
  with a position,
  download the place names once with **Download place names** at the
  foot of the Manage page (or `python make_places.py --download`, about
  10 MB from geonames.org): the Place line then names the town nearest
  to each -- "Hirtshals, North Denmark, Denmark (2 km)" -- and searches
  like `hirtshals`, `place:denmark` or `calgary` find them. `has:gps`
  lists the six with a position.
- With the street maps downloaded, **Find named places** (it also runs
  after a download of street maps) names the motel itself for 19: its
  Place line then starts with the motel's name, before "California,
  United States", and searching that name finds it.

The nearest town is the nearest in the place names, which list towns of
1,000 people or more: 03, taken at Lake Serene, shows "Gold Bar,
Washington, United States (12 km)". 11 was taken in Cimadera,
Switzerland, a few km from the Italian border, where the nearest town
listed is Italian; its file names its place, and a picture's own names
come first, so it reads "Cimadera, Ticino, Switzerland" (its pin is its
geotag).

When you are done, remove the library on the Manage page (deleting its
indexed data) and delete the copied folder.
