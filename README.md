# Image Database

A searchable database of your images. It scans folders of pictures, asks an
LLM to describe each one (including any text in the image), and lets you search
those descriptions from a web page.  You can see a [demo of the site this produces here](https://seligman.github.io/search_image_sample/index.html).

The LLM can be a model on your own computer (llama.cpp, LM Studio, vLLM,
Ollama) or a cloud service (OpenAI, Azure OpenAI, Gemini, OpenRouter,
Anthropic's Claude). A library can also be published as a static website.

Supported image types are JPG/JPEG, PNG, GIF, WebP, TIFF, BMP, HEIC/HEIF,
AVIF (HEIC needs `pillow-heif`; AVIF needs a Pillow built with AVIF support)
and Insta360 `.insp` photos. RAW files (`.dng`, `.arw`, ...) are not indexed.

360-degree photos are recognised: stitched panoramas (2:1 images tagged as
equirectangular, or from a known 360 camera) and unstitched Insta360 frames
holding two fisheye circles. The latter are stitched into a panorama for the
preview (and for the LLM), and thumbnails show a normal-looking view of the
front of the scene. Each 360-degree photo larger than its 2048 px preview
also gets a panorama up to 4096 px wide (made from the full-size original)
so the 360 view stays sharp.

Coming from an older version? Read [Upgrading from an older
version](#upgrading-from-an-older-version) first.

## Install

You need Python 3.9 or newer and a few packages:

    python -m pip install -r requirements.txt

`pillow-heif` is only needed to scan libraries that contain HEIC images.
Without it the scanner still runs and skips HEIC files with a warning. Each
script also prints an install command if a package is missing.

Run the commands in this README from the program folder (the folder that
holds `serve.py` and the other scripts).

### Where your settings and data are kept

By default `config.json` (the settings) and the `data/` folder (the
database, thumbnails, previews, logs and exports; see [The data
folder](#the-data-folder)) are kept in the program folder, where git
ignores them. They describe your pictures in detail, so keep them private.
To keep them somewhere else, set the `SETTINGS_BASE` environment variable
before running any tool:

    SETTINGS_BASE=/path/to/store python serve.py

or, on Windows (in PowerShell, `$env:SETTINGS_BASE = "c:\path\to\data"`):

    set SETTINGS_BASE=c:\path\to\data
    python serve.py

## Quick start

1. Start the server and open the address it prints
   (`http://127.0.0.1:5000`):

       python serve.py

2. On the **Settings** page (linked in the page header), press **Add
   endpoint**, pick the service and the model and, for a cloud service,
   where its API key comes from (see [Describing
   pictures](#describing-pictures)). Press **Test**: it describes a bundled
   sample picture and shows the answer.
3. On the **Manage** page, press **Add library** and choose the folder that
   holds your pictures (type it, or use **Browse...**). The folder must be
   outside the program folder and the data folder. Leave **Scan and
   describe it right away** ticked.
4. Watch the job panel. Pictures can be found by file name, date and
   camera as soon as they are scanned, and by what they show once they are
   described. The job keeps running when you close the page (but not when
   you stop the server); **Process all libraries** scans and describes
   every library again after you add pictures.
5. Open **Search** in the page header and type what you are looking for.

To try it on the 18 bundled sample pictures first, see
[sample/README.md](sample/README.md).

The same from the command line: `python settings.py` adds endpoints and
libraries from a numbered menu (it takes no options), `python scan.py`
scans and describes (see [Scanning](#scanning)), and `python serve.py`
serves the search page.

## Describing pictures

A library is described by the endpoint it names, or else by the default
endpoint (the first one you add, until you press **Make default** on
another). You can set up several and compare them on your own pictures
(see [Comparing models](#comparing-models)).

### On your own computer (llama.cpp)

Nothing leaves your computer. Any vision model your server can run works;
models that read text well (OCR) give the best results on screenshots and
documents. Start llama.cpp's `llama-server` with the model and its vision
projector (the `mmproj` file published with a vision model):

    llama-server -m model.gguf --mmproj mmproj.gguf --parallel 4 -c 32768

Then add an endpoint with the **llama.cpp** service; its default address,
`http://127.0.0.1:8080/v1`, is where `llama-server` listens. Each slot
(`--parallel`) describes one picture at a time, and the slots share the
context (`-c`): give each about 8192 tokens. The Settings page shows
whether the server is running and which model it loaded. To run the model
on another computer, start `llama-server` there with `--host 0.0.0.0` and
use that computer's address in the endpoint.

As a rough guide at 4-bit quantization: a model of about 4B parameters
suits a GPU with 8 GB of memory, 8B to 12B suits 12 to 16 GB, and larger
models need 24 GB or more. Without a GPU it works, slowly. LM Studio, vLLM
and Ollama work too: pick their service when you add the endpoint, and load
a vision model.

The helper module can start the server before each describe run and stop
it afterwards (see [helper](SETTINGS.md#helper) in SETTINGS.md).

### With a cloud service

Pick the service (OpenAI, Azure OpenAI, Gemini, OpenRouter or Anthropic's
Claude), the model, and where the API key comes from: an environment
variable (recommended, for example `OPENAI_API_KEY`; set it before you
start `serve.py` or `scan.py`), a file that holds only the key (keep it
outside the program folder), or typed in (stored in `config.json`). Keys
are never shown, logged or stored with the descriptions.

Your pictures are then sent to that company: shrunk (1536 px on the long
edge by default), without file names, folders or EXIF data. Each one costs
tokens; add the endpoint's prices and **Test** shows the cost per picture.
Before the first describe of a library that uses a cloud service, the
Manage page says how many pictures will be sent and where. A cloud service
whose API key is not set up is never sent a picture. To try a few pictures
first, run `python scan.py --limit 20`.

Hidden pictures (see [Searching](#searching)) are described too, so they
are sent to the cloud service like the others: hiding only keeps them out
of searches and exports. To keep pictures away from a cloud service, give
their library a local endpoint, or skip them with `ignore` patterns (see
SETTINGS.md).

### What a description holds

The LLM replies with a JSON structure that is checked and stored: a title,
a description, the visible text (transcribed), the kind of picture (photo,
screenshot, document, ...) and of document (receipt, chat, menu, ...), the
setting, scene, time of day and occasion, how many people there are (never
who they are), animals, food, vehicles, colors, search tags, quality flags
(blurry, dark, ...), whether the picture is sensitive or shows private
information, and scores from 1 to 5. Libraries set to extended metadata also
get counted objects, activities, landmarks, a location guess and the mood.
A library's context notes (such as "Photos from a sailing club") are sent
with each of its pictures. Each description records which model and
endpoint made it, how long it took and how many tokens it used.

Several images are described at once: as many as the endpoint's concurrency
allows (for llama.cpp, its slot count). Rate limits and busy servers are
waited out, an unusable reply is asked for again, and an image that still
fails is marked and retried on later runs (up to three times). A rejected
API key or an empty account stops the run with a message.

## Scanning

The Manage page's buttons and `python scan.py` do the same work. A scan
finds new and changed images and reads each of them once: it stores their
metadata, EXIF data, capture date, camera and GPS position in a SQLite
database (`data/index.db`), builds a thumbnail and a 2048 px preview (about
0.5 MB per picture in `data/previews/`; see `previews` in SETTINGS.md), and
then the LLM describes anything without a description. Run it again
whenever your folders change; unchanged files are not read again.

Descriptions follow the pictures: a file that is touched, renamed or moved
keeps its description, and an exact copy of a described picture gets the same
description. A file that disappears is first marked missing and only removed
after `purge_after_days` (30 by default), so it comes back as it was if the
file returns (a library on a share that is offline is skipped altogether). If
most of a library seems to have vanished at once, nothing is marked; run
`python scan.py --library NAME --allow-mass-removal` if that really was
intended.

### From the command line

    python scan.py

Options:

- `--no-describe` indexes without calling the LLM; `--no-scan` only
  describes.
- `--library NAME` limits the run to one library (repeat it for more).
- `--newest-first` / `--oldest-first` choose the order images are described
  in (by default each library's `newest_first` setting).
- `--retry-failed` also retries images the LLM failed on three times.
- `--redescribe-stale` also describes again images described with an older
  prompt (after the new ones). Older descriptions stay searchable until then.
- `--limit N` describes at most N images in this run.
- `--endpoint NAME` describes with this endpoint (or group) instead of each
  library's own.
- `--list-endpoints` lists the configured endpoints and where their keys come
  from; `--test-endpoint NAME [--image PATH]` describes one picture (a
  sample by default) and prints the result. Neither ever shows a key.
- `--verbose` lists every added or changed file (by default only the first
  20 of each kind per library, then a count).
- `--allow-mass-removal` marks files missing even when most of a library
  seems to have vanished.

`scan.py` exits with 0 when it finished or was stopped as asked (see
below; 130 after a second Ctrl+C), 1 when it could not start (another job
is running, or an `abort` file is already there) or the describe step
stopped early (a rejected key, or no usable endpoint left), and 2 for a
settings problem (an unknown `--endpoint`, say), so scheduled runs can
tell them apart.

To stop a long scan early, press Ctrl+C, or create a file named `abort` in
the data folder (`data/abort`), or one named `abort` or `abort.txt` in the
directory you ran `scan.py` from. It finishes the images in progress, saves
its progress, and exits (a second Ctrl+C stops it at once; what was saved
so far is kept). Delete the `abort` file and run `scan.py` again to pick up
where it stopped.

## Searching

    python serve.py

This starts a local web server. Open the printed address in a browser. The
search page shows a grid of thumbnails that filters as you type, best
matches first (newest first when there is nothing to look for), with the
months labelled when sorted by date (months of a few pictures share a row).
The grid keeps growing as you scroll; there is no limit on the number of
results. With several libraries you can search all of them or pick one in
the page header or the filters.

- **Filters** (the sidebar; on a phone a drawer, which also holds the sort
  order and the other view settings) count the matching pictures by
  library, type, people, setting, time of day, event, year (and month),
  camera, tags, subjects, animals, food, vehicles and colors.
  Values of one filter are alternatives, different filters must all match;
  the chosen ones show above the results with a button to remove each.
  "Hide blurry and accidental shots" and "Hide sensitive pictures" (on by
  default: explicit and graphic pictures) are there too.
- **Group similar** shows near-duplicates (and exact copies) within each
  library as one stack with a "x3" badge; click the badge to see them all
  (copies in two different libraries show separately). The stack shows its
  best picture: the best looking and sharpest, then the largest, then an
  original (with a camera name and the earliest date) rather than a copy.
  Pictures whose camera (or file name) dates them are only stacked when
  they were taken the same day (or within 12 hours across midnight), and
  then rather with each other than with a look-alike dated only by its
  file; pictures dated only by their file need a closer match. Shots of a
  burst or an exposure bracket (taken within two seconds) may differ a
  little more. Documents and screenshots are only stacked with exact
  copies, as different pages look alike to the comparison.
- **The viewer** opens when you click a thumbnail: the picture at screen
  size, arrow keys (or swiping) for the next and previous result, zoom with
  the wheel, a pinch or a double click, and the information pane (`i`):
  date, camera settings, the description and text in the picture with your
  search words marked, clickable tags and other facts (each starts a search),
  quality, and "More like this". 360-degree photos can be looked around in
  by dragging (the flat panorama when the browser cannot show that).
  "Details page" opens the same on a page of its own, to link to.
- **The address** holds the search, the filters, the sort order and the open
  picture, so links, reloads and the Back button bring the same view back.
- **Keyboard**: `/` jumps to the search box, `Esc` clears it or closes the
  viewer, arrow keys move between thumbnails and `Enter` opens one.
- **On this computer** (where the Manage page works: a server started
  without `--host`) the viewer can also copy the picture's file path, show
  it in the file manager, describe it again, and hide it. Hidden pictures
  no longer show in searches and are never exported, but they are still
  described (and sent to a cloud service if their library uses one);
  "Show hidden pictures" in the sidebar brings them back to unhide them.
  **Select** chooses many pictures at once (Shift-click for a range, or
  "Select all") to hide, unhide or copy their file paths.
- The **Map** shows the located pictures of the current search as pins; a
  pin with a number holds pictures taken close together, which come apart
  as you zoom in. Click a pin for a list of its pictures: click one to view
  it, or **Show these in the grid** for a search page with just those
  pictures, from where **Back to the map** returns to the same place. Drag
  or use the arrow keys to move the map; the wheel, `+` and `-` or a double
  click zoom, `Enter` lists the pictures of the pin nearest the middle
  (ringed while the map has the keyboard focus), and `Esc` closes the list.
  The address keeps the search, the map's place and the open list, so links
  and reloads show the same view (after a new search the map fits its
  pictures); a static export with the map works the same way. The map uses
  a small bundled world image; run `python make_tiles.py` once for a
  detailed one (see [Other tools](#other-tools)).
- While some pictures are not described yet, a note above the results says
  how many: they can only be found by file name, date and camera until then.
- When nothing matches, the page says what left the pictures out (a filter,
  "Hide sensitive pictures", the library) with a button to change it, or
  suggests a spelling.

### Search syntax

Type words to find the pictures that have all of them: in the title,
description, tags, subjects, the text in the picture and the other fields.
Capitals, accents and plurals do not matter (`cafe` finds "Café", `cats`
finds "cat"). While you are still typing, the last word also finds longer
words that start with it (`sunse` finds "sunset"); after `Enter`, and in
links, only whole words count. Small words such as "the" and "of", and
filler such as "my", "show me" or "pictures of", are skipped
(`pictures of my dog` searches for `dog`). A word in the title counts more
than a word in the description, a year (`receipt 2019`) prefers pictures
taken that year, and pictures that contain your words in the same order
come first.

- `"red car"` - the words together, in this order
- `beach -dog` - leave out pictures with a word (or `-"a phrase"`)
- `cat OR dog` (or `cat or dog`) - either word
- `tag:beach` - a word in one field: `tag`, `subject`, `title`, `text` (the
  text in the picture), `description`, `scene`, `animal`, `food`,
  `vehicle`, `color`, `camera`, `folder`, `file`
- `type:screenshot` - a kind of picture: `photo`, `screenshot`, `document`,
  `scan`, `artwork`, `receipt`, `chat`, `whiteboard`, `menu`, `ticket`, ...
- `people:none` - how many people: `none`, `1`, `2`, `3-5`, `6-10`,
  `crowd` or `any`; `beach no people` works too, and the word `people`
  finds every picture with people in it
- `year:2019`, `year:2015-2019` - taken in those years
- `before:2019-06`, `after:2019` - taken before June 2019, or from 2020 on
  (`YYYY`, `YYYY-MM` or `YYYY-MM-DD`)
- `has:text`, `has:gps` - with text in the picture, with a GPS position
  (also `has:people`, `has:animals`)
- `is:360`, `is:duplicate`, `is:undescribed` - 360-degree photos, pictures
  with near-duplicates in the same library, pictures not described yet
- `lib:name` - only the pictures of one library

### Searching from other computers

By default the server only answers this computer. To let the other
computers on your network search too, start it with `--host` (add
`--port N` for another port than 5000, which also helps when 5000 is
taken):

    python serve.py serve --host 0.0.0.0

The others then open `http://NAME:5000/`, where NAME is this computer's
name or one of its addresses (the server prints them when it starts). Any
other name, such as an alias your router hands out, is refused with
"Unknown host name.".

There is no login. Anyone who can reach the computer can search every
library that is turned on, see file and folder names, dates, GPS positions
and the map, camera EXIF data and the model that described each picture,
download the original files with all their metadata, and turn off "Hide
sensitive pictures". Hidden pictures and turned-off libraries stay out. In
this mode Manage, Settings and jobs are off, on this computer too; to manage
the libraries at the same time, run a second server without `--host` on
another port (`python serve.py serve --port 5001`). To share pictures
beyond your household, publish a library instead (see [Publishing a
library](#publishing-a-library)): it leaves out what you choose.

## Managing libraries in the browser

The header of every page links to **Manage** and **Settings** when the
server runs on this computer (the default address, `127.0.0.1`). Most of
what the command line does can be done there:

- **Manage** lists each library with its folder, whether the folder can be
  reached, how many pictures are described, waiting, out of date or failed,
  and the last job. Its buttons scan a library, describe it, do both, retry
  failed pictures, re-describe pictures described with an older prompt, and
  build missing previews. **Process all libraries** does a scan and describe
  of every library that is turned on. **Export...** publishes a library as
  a static website (see "Publishing a library").
- **Add library** asks for the folder (type it, or use **Browse...**; a
  network share is typed as `\\server\share`), suggests a name, shows how
  many pictures it found, and previews which files the ignore patterns skip.
  Editing a library can rename it or point it at a moved folder; its
  descriptions are kept. **Remove** asks you to type the name and whether to
  keep the indexed data or delete it.
- Indexed data that belongs to no library (a library removed with its data
  kept, or taken out of `config.json` by hand) is shown in a notice at the
  top of Manage, with **Re-attach to...** (give it to a library, say one
  you added again for the same folder) and **Delete...**.
- The job panel shows the running job with its progress, speed and time
  left, with **Pause**, **Resume** and **Cancel**; the queue; recent jobs;
  and the log. Jobs keep running when you close the page, one at a time, and
  a job cut short by stopping the server can be resumed. A scan started
  from the command line is shown too, with a button that asks it to stop.
  The queue is kept in `data/jobs.db`. When two servers use the same data
  folder, the first one runs the jobs and the other only shows them.
- **Settings** lists the endpoints with their status (a local server shows
  whether it is running and which model it loaded), where each API key comes
  from (never the key itself), and buttons to add, edit, test, remove and
  make one the default. **List models** in the endpoint dialog asks the
  server for its models; before the endpoint is saved, it only uses a key
  from a file or an environment variable when a saved endpoint already
  sends that key to the same address (or the address is the cloud
  service's own), so save the endpoint first otherwise. **Test** describes
  the bundled sample picture and shows the answer, the time it took and,
  with prices set, the cost per picture. Before the first describe of a
  library that uses a cloud service, the page says how many pictures will
  be sent and where. A cloud service whose API key is not set up is never
  sent a picture: Manage says what is missing and keeps the describe
  buttons off.

Only the command line can let a scan mark most of a library missing
(`scan.py --allow-mass-removal`; a scan from Manage that stops for this
says so in its log), describe at most N pictures (`--limit`), describe with
another endpoint for one run (`--endpoint`), list every changed file
(`--verbose`), and set the helper module (`settings.py`, or `helper` in
`config.json`).

These pages only answer requests from this computer, and are turned off
when the server is started with `--host` (see [Searching from other
computers](#searching-from-other-computers)). They have no login and are
not a protected admin area for several users: any program running on this
computer, under any account, can use them (web pages on other sites
cannot; the browser marks their requests, which are refused). Only run the
server on a computer you trust. Do not put the server behind a reverse
proxy on the same computer: every visitor would then look local.

## Publishing a library

A library can be exported as a static website: the same search, viewer and
detail pages, searching in the browser with no server. It works on any
static web host, and when opened from disk (double-click `index.html`).

On the Manage page, **Export...** on a library opens a dialog:

1. **Options**, in plain words: web-size pictures, originals (with their
   hidden details removed) or thumbnails only; what to leave out
   (screenshots, documents, scans, pictures flagged as private or sensitive,
   pictures not checked yet); whether to also publish file names, folder
   names or locations; and the site's title, a footer line, the output
   folder, a zip file and search-engine indexing. The line at the bottom
   counts the pictures that would be published.
2. **Review what will be published**: how many pictures, the estimated
   size (and a warning when the folder's disk has less room, or when the
   library's folder is offline and pictures need it), how many are left
   out and why, and the pictures the AI model flagged, grouped by reason.
   Untick **Publish** to leave a picture out, or tick it to publish one the
   options leave out; a picture opens in a new tab to read its
   description. A picture you chose to publish is left out again when it
   gets a new flag later (say it is described again and shows private
   information), and the review says so.
3. **Export** saves the options with the library (`export` in
   `config.json`) and runs the export as a job: the dialog and the job
   panel show its progress (**Stop export** stops it). When it is done the
   dialog shows the folder, **Open folder**, and how to publish it. The
   library card then shows the last export, with **Open folder** and **How
   to publish**.

The same from the command line:

    python export.py --library Family
    python serve.py package --library Family site_example

`serve.py package` without `--library` exports the only enabled library.
One export runs at a time (`data/export.lock`), whether started on the
Manage page or from the command line. Both print what was left out and
why, and exit with status 1 when pictures could not be exported (2 when
the export was stopped). Ctrl+C stops `export.py` at the next safe point,
keeping what is done (a second Ctrl+C stops it at once); run the same
command again to finish the export.

The site goes to the folder given (the dialog's Folder, `--out` for
`export.py` or the folder after `package`). Without one, the command line
writes to `data/exports/<library name>/` in the data folder, even when the
dialog last used another folder. That folder's name is the library name in
lower case, with dashes for spaces and other signs ("Summer Trip" goes to
`summer-trip`); when two libraries' names give the same folder, the
library's id is added to it, and a library keeps the folder it was last
exported to, so adding such a library later does not move it. The folder
must be new, empty, or an earlier export of the same library: an export
marks the folder it writes (a `.search-images-export` file holding only a
random id) and later removes only files it made itself (the list is kept
in `data/exports/state/`, never in the site). Each library needs a folder
of its own. Inside the program folder only `site/`, `site_<name>/` and
`exports/` are accepted (git ignores those), so your photos never end up
in a commit. Re-running an export only rewrites what changed. An export that
cannot read any picture (say the library's drive is not connected, and
originals were asked for) stops before it changes the folder, and one that
cannot write (a full disk, no access) stops at once; both say so in plain
words.

What is published follows the library's `export` options (see SETTINGS.md),
which the Export dialog saves. `export.py` can change them for one run:
`--images` picks the kind of pictures, and the other switches can only
publish more. To publish less, change the options in the Export dialog (or
`export` in `config.json`). `serve.py package` always uses the saved
options.

- `--images web` (the default) publishes 2048 px pictures, `original` the
  originals with their metadata removed (HEIC, HEIF, AVIF and TIFF become
  JPEG, BMP becomes PNG; an unstitched 360-degree photo, with its two
  fisheye circles, is published as the stitched panorama), and `none`
  thumbnails only.
- Left out unless asked for: pictures not checked yet
  (`--include-undescribed`: not described yet, or described by an older
  version that did not look for private or sensitive content), screenshots
  and documents (`--include-screenshots`, `--include-documents`; a picture
  counts as a document or a screenshot when the AI model saw a receipt,
  letter, form, ticket, handwriting or a chat, web page or app on it,
  whatever kind of picture it called it), pictures flagged as private or
  sensitive (`--include-private`, `--include-sensitive`), file names
  (`--file-names`), folder names (`--folders`), and GPS positions with the
  map (`--location`, which also adds the AI model's separate landmark and
  place-guess fields). Hidden pictures are never published.
- Titles, descriptions, tags and the text read from pictures are published
  as the AI model wrote them: they can name people or places a picture
  shows, whatever the options say. Read through what you publish.
- Never published: paths, file times (a picture without a date from the
  camera or its file name is published without a date), content hashes,
  the model that described the pictures and its costs, other libraries, or
  endpoint settings. The pictures carry no EXIF, XMP, IPTC or text
  metadata (an original JPEG keeps its orientation; a colour profile keeps
  its colours but loses its texts), and their ids do not reveal the
  database's. Each picture shows its title, description, tags, the text
  read in it, the day it was taken and the camera model and settings.
- `--zip` also writes `<folder>.zip` next to the folder. A file of that
  name is only replaced when an earlier export made it and it was not
  changed since; otherwise the export stops before writing anything. The
  pages ask search engines not to list them or their pictures (and a
  `robots.txt` says the same) unless `allow_indexing` is set, but anyone
  who has the address can open the site.

Try the site with any static web server (the export prints the command for
its folder), for example:

    python -m http.server --bind 127.0.0.1 --directory data/exports/family

and open http://localhost:8000/ (with `--bind 127.0.0.1` only this computer
can open it). To publish it, copy everything in the folder to any host
that serves plain files (GitHub Pages, Netlify, your own web server);
nothing needs to be installed there. Export again after
describing more pictures: only what changed is written.

Opened from disk, the 360-degree view shows the flat panorama (browsers do
not allow it for files); over http it can be turned around.

## Comparing models

    python compare_models.py --endpoints local,cloud --sample 100 --endpoint-limit cloud=20

Sends the same sample of your indexed images to several endpoints, one
endpoint at a time, and writes a side-by-side HTML report (titles,
descriptions, tags, text, types, flags, scores, time and tokens per image,
plus a summary per endpoint with images per minute and an estimated cost
per 10,000 images) and a JSON file with the raw results to `data/reports/`.
The database is only read; nothing is stored. With a cloud endpoint, the
sample is sent to that service and paid for.

- The sample is seeded (`--seed`), so a run can be repeated, and mixes the
  libraries and likely screenshots or documents with photos so that every
  first part of it is a fair mix too. `--endpoint-limit NAME=N` gives an
  endpoint only the first N images, to keep cloud costs down. `--ids 1,2,3`
  picks the images, `--library NAME` the libraries, `--profile extended` the
  extended metadata.
- Libraries whose own `endpoint` setting keeps them on local endpoints are
  not sent to a cloud endpoint unless you name them with `--library`. An
  endpoint whose first few images all fail (or that keeps failing) is not
  sent the rest of the sample.
- Local servers that cannot run at the same time can be compared one by
  one: run the script once per endpoint with the same `--seed` and
  `--sample`, then combine the JSON files each run names on its `Results:`
  line:

      python compare_models.py --merge data/reports/compare-<first>.json data/reports/compare-<second>.json

  The combined report goes to `data/reports/` too. `--out PATH` chooses
  another file (relative to the current folder); keep it out of the
  program folder, or in `data/reports/`.
- The helper module is not used: start the servers yourself.

The reports contain thumbnails and text from your pictures; keep them
private.

## Settings, tools and files

### Settings

Everything is kept in `config.json`: the endpoints, the libraries and a few
general settings. The Settings and Manage pages edit most of it, and so
does `python settings.py`; [SETTINGS.md](SETTINGS.md) describes every field,
including the two only a text editor changes (`reader_threads` and
`purge_after_days`).

`settings.py` writes nothing until you choose **Save and exit**. If
`config.json` was changed in the meantime (on the Manage or Settings page,
or by hand), it says so and asks whether to save over those changes,
reload the file (dropping the changes made in `settings.py`) or exit
without saving; when nothing was changed in `settings.py`, the file is
left as it is.

### The data folder

`data/`, next to `config.json`, holds what the tools make. Most of it is
private:

- `index.db` - the database: file names, folders, dates, camera, EXIF and
  GPS data, and the descriptions.
- `thumbs.db` and `previews/` - thumbnails, and 2048 px previews and 360
  panoramas (about 0.5 MB per picture).
- `jobs.db` and `logs/` - the Manage page's jobs and their logs, which name
  files. With the `LOG_ERRORS` environment variable set, the LLM's replies
  to failed requests are saved in `logs/` too.
- `exports/` - static sites (`exports/<library name>/`) and the list of
  files each export wrote (`exports/state/`).
- `reports/` - model comparison reports.
- `cache/` (near-duplicate groups, rebuilt when missing) and `tmp/`.
- `tiles/` and `blue_marble.jpg` (from `make_tiles.py`), `world_map.png`
  (from `world_map.py`).
- `images.db` and `thumbs/` - the database of older versions (see
  [Upgrading](#upgrading-from-an-older-version)).
- `*.lock` files and `abort` - used while tools run.

To keep the descriptions, back up `config.json` and `data/index.db` while
no tool is running.

### Other tools

    python progress.py counts
    python progress.py files

`counts` shows per library how many pictures are described, out of date,
waiting, failed or missing; `files` lists the pictures that still need a
description (new, failed or out of date), with the reason for each
failure.

    python world_map.py [out.png]

draws a world map with a pin for each place pictures were taken (by default
`data/world_map.png`).

    python make_tiles.py

downloads a detailed world image (about 27 MB, to `data/blue_marble.jpg`)
and cuts it into map tiles in `data/tiles/`, for the Map page and for
exports with locations. Without it the map shows a small bundled image. Run
it again after an interruption: it keeps the tiles that are whole and
writes the missing or damaged ones.

### Program files

- `serve.py` - search server (and `package`, a shortcut to `export.py`)
- `settings.py` - manage settings from a menu
- `scan.py` - scan libraries, then request descriptions
- `describe.py`, `prompts.py` - the describe step and the prompt it sends
- `export.py` - publish one library as a static website
- `compare_models.py` - compare how endpoints describe the same images
- `progress.py`, `world_map.py`, `make_tiles.py` - see [Other
  tools](#other-tools)
- `example_helper.py` - the hooks a helper module can have
- `search_index.py` - the compact search records the search page downloads
  and searches in the browser, and the groups of near-duplicate pictures
- `admin.py`, `library_ops.py` - the Manage and Settings pages' server side
  (library changes, folder checks, jobs, endpoints)
- `static/`, `templates/` - the web pages (`shell.js` is the header shared
  by every page; `site.js` is where the pages get their data, from the
  server or from a static export; `search-core.js` is the search engine and
  `viewer.js` the picture viewer, both shared with a static export;
  `app.js`, `detail.js` and `map.js` run the search, detail and map pages;
  `manage.js` and `settings.js` run Manage and Settings, and
  `export-review.js` holds the rules and words of the Export dialog)
- `common.py`, `db.py`, `cache.py`, `jobs.py`, `images.py`, `imageinfo.py`,
  `llm.py` - shared helpers (settings, database, thumbnails and previews,
  job control, image reading, EXIF facts, LLM calls)
- `sample/` - pictures to try the project on
- `SETTINGS.md` - reference for every field in `config.json`

## Upgrading from an older version

The first run of any tool (`serve.py`, `scan.py`, `settings.py`,
`export.py`, ...) copies the old `data/images.db` and `data/thumbs/` into
the new `data/index.db` and `data/thumbs.db`. The old files are never
changed, so an older checkout can still use them; delete them once you no
longer need one. Libraries are matched by name, so do not rename a library
in `config.json` by hand before that first run (renaming it with
`settings.py` is fine). Each library then gets a permanent `id` in
`config.json`; renaming a library later only changes its name.

Other changes to be aware of:

- The first scan after upgrading reads every picture once more, to fill in
  details the old database did not keep and to build the previews (about
  0.5 MB per picture, so about 50 GB per 100,000 pictures, in
  `data/previews/`; with `"previews": false` on a library the pictures are
  still read, but no previews are kept). On a large library on a network
  share this can take hours. It can be stopped at any time (**Cancel** on
  Manage, or Ctrl+C or `data/abort` for `scan.py`) and picks up where it
  stopped.
- The prompt is now version 5, with many more fields (see [What a
  description holds](#what-a-description-holds)). Images described by an
  older version keep their descriptions, which still show and search as
  before; run **Re-describe out of date** on Manage (or
  `scan.py --redescribe-stale`) when you want them redone. Until then an
  export leaves them out, as they were not checked for private or
  sensitive content (include them with "pictures not checked yet" in the
  Export dialog, or `export.py --include-undescribed`).
- `serve.py package` used to put every library in one site (`site/`, or
  `data/exports/site/` in later versions) and took `--force`. It now
  exports one library (`--library NAME` when more than one is turned on)
  to `data/exports/<library name>/`, and `--force` is gone. To update the
  old site in place, name its folder: a site made by the older command
  (`data/exports/site/`, or a `site/` from the first versions) is
  recognised and its files are replaced; other files in the folder are
  kept. Screenshots, documents, file names, folders and GPS positions are
  now left out unless you include them, and the pages ask search engines
  not to list them unless `allow_indexing` is set. See
  [Publishing a library](#publishing-a-library).
- The older `endpoint` setting (one endpoint, or a round-robin list) still
  works. Editing endpoints in `settings.py` saves them in the newer
  `endpoints` form.
- `config.json` is now written with non-ASCII characters escaped (`\u00fc`),
  and read as UTF-8. A file saved in the system's encoding still loads, with a
  warning.
- A relative `helper` path is looked up next to `config.json` first; the old
  location (the current directory) still works, with a warning.
- The helper's `call_api(prompt, image_path)` now gets a temporary JPEG of the
  image as it will be sent. Add a third parameter, `source_path`, to also get
  the original file's path (see `example_helper.py`).
- NAS and system junk folders (`@eaDir`, `#recycle`, `.thumbnails`, ...) are
  skipped by default (`default_ignores`); images indexed there before are
  marked missing.
- Symbolic links and directory junctions inside a library are not followed.
  Images indexed through them before are kept as they are; add the link's
  target as a library of its own to index it.
- A library can no longer be added (or moved) to a folder inside the
  program folder or the data folder; one that is already there keeps
  working. To try the bundled sample pictures, copy them elsewhere first
  (see [sample/README.md](sample/README.md)).

## Tests

    python -m unittest discover -s tests

The page scripts' own tests (`tests/js/`) run as part of this when Node.js
is installed, and are skipped when it is not.

`tests/test_repo_hygiene.py` checks every file git would commit for
personal details (absolute paths, host names, model files). To add your own
private words to that check (a NAS or share name, library names), list them
one per line in `private-terms.txt` next to `config.json` (in the
`SETTINGS_BASE` folder, or the program folder by default) or in
`.git/info/private-terms`. Git never commits either file.

## License

The code is released under the MIT License; see [LICENSE](LICENSE). The
sample images are covered by their own licenses, listed in
[sample/images/CREDITS.md](sample/images/CREDITS.md).

## Credits

`static/worldmap.jpg` is an equirectangular projection derived from NASA's
Blue Marble imagery and is in the public domain. Source:
https://commons.wikimedia.org/wiki/File:Equirectangular-projection.jpg
