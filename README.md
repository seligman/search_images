# Image Database

A searchable database of your images. It scans folders of pictures, asks an
LLM to describe each one (including any text in the image), and lets you search
those descriptions from a web page.  You can see a [demo of the site this produces here](https://seligman.github.io/search_image_sample/index.html).

Supported image types are JPG/JPEG, PNG, GIF, WebP, TIFF, BMP, HEIC/HEIF,
AVIF (HEIC needs `pillow-heif`; AVIF needs a Pillow built with AVIF support)
and Insta360 `.insp` photos. RAW files (`.dng`, `.arw`, ...) are not indexed.

360-degree photos are recognised: stitched panoramas (2:1 images tagged as
equirectangular, or from a known 360 camera) and unstitched Insta360 frames
holding two fisheye circles. The latter are stitched into a panorama for the
preview (and for the LLM), and thumbnails show a normal-looking view of the
front of the scene.

## Where files are stored

By default `config.json` and the `data/` folder live next to the scripts. Set
the `SETTINGS_BASE` environment variable to keep them somewhere else:

    SETTINGS_BASE=/path/to/store python scan.py

## Requirements

Python 3, plus a few third party packages:

    python -m pip install -r requirements.txt

`pillow-heif` is only needed to scan libraries that contain HEIC images.
Without it the scanner still runs and skips HEIC files with a warning. Each
script also prints an install command if a package is missing.

## Setup

The easiest way is the browser: run `python serve.py`, open the printed
address, and use the **Settings** page to add an endpoint and the **Manage**
page to add your libraries (see [Managing libraries in the
browser](#managing-libraries-in-the-browser)). The console settings tool does
the same from a numbered menu:

    python settings.py

From there you can:

- Add the LLM endpoints that describe your images: a local server
  (llama.cpp, LM Studio, vLLM, Ollama) or a cloud service (OpenAI, Azure
  OpenAI, Gemini, OpenRouter, Anthropic's Claude), each from a preset. Pick
  where its API key comes from (an environment variable, a key file, or
  typed in), and test it on a sample picture.
- Set an optional helper module that wraps the LLM calls. See
  `example_helper.py`.
- Add the image libraries to monitor. Each library has a name and a folder,
  and can use its own endpoint, ask for extended metadata, and carry short
  context notes for the LLM.

Settings are stored in `config.json`. See [SETTINGS.md](SETTINGS.md) for the
full format, including options the menu does not edit (such as per-library
ignore patterns and extra request fields).

With a cloud endpoint, your pictures (shrunk, without file names or EXIF
data) are sent to that service. A local server keeps everything on your own
machines. For a local model, any vision model your server can run works;
models with good text reading (OCR) give the best results on screenshots
and documents.

## Scanning

    python scan.py

This finds new and changed images and reads each of them once: it stores
their metadata, EXIF data, capture date, camera and GPS position in a SQLite
database (`data/index.db`), builds a thumbnail and a 2048 px preview, and then
asks the LLM to describe anything without a description. Run it again
whenever your folders change; unchanged files are not read again.

The LLM replies with a JSON structure that is checked and stored: a title,
a description, the visible text (transcribed), the kind of picture (photo,
screenshot, document, ...) and of document (receipt, chat, menu, ...), the
setting, scene, time of day and occasion, how many people there are (never
who they are), animals, food, vehicles, colors, search tags, quality flags
(blurry, dark, ...), whether the picture is sensitive or shows private
information, and scores from 1 to 5. Libraries set to extended metadata also
get counted objects, activities, landmarks, a location guess and the mood.
Each description records which model and endpoint made it, how long it took
and how many tokens it used.

Several images are described at once: as many as the endpoint's concurrency
allows (for llama.cpp, its slot count). Rate limits and busy servers are
waited out, an unusable reply is asked for again, and an image that still
fails is marked and retried on later runs (up to three times). A rejected
API key or an empty account stops the run with a message.

Descriptions follow the pictures: a file that is touched, renamed or moved
keeps its description, and an exact copy of a described picture gets the same
description. A file that disappears is first marked missing and only removed
after `purge_after_days` (30 by default), so it comes back as it was if the
file returns (a library on a share that is offline is skipped altogether). If
most of a library seems to have vanished at once, nothing is marked; run with
`--allow-mass-removal` if that really was intended.

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

`scan.py` exits with 0 when it finished, 1 when the describe step stopped
early (a rejected key, or no usable endpoint left) and 2 for a settings
problem (an unknown `--endpoint`, say), so scheduled runs can tell them
apart.

To stop a long scan early, create a file named `data/abort` in the data
folder (or `abort` or `abort.txt` in the directory you ran `scan.py` from). It
finishes the images in progress, saves its progress, and exits. Delete that
file and run `scan.py` again to pick up where it stopped.

### Upgrading from an older version

The first run of any tool (`scan.py`, `serve.py`, `settings.py`,
`progress.py`) copies the old `data/images.db` and `data/thumbs/` into the new
`data/index.db` and `data/thumbs.db`. The old files are never changed, so an
older checkout can still use them. Libraries are matched by name, so do not
rename a library in `config.json` by hand before that first run (renaming it
with `settings.py` is fine). Each library then gets a permanent `id` in
`config.json`; renaming a library later only changes its name.

Other changes to be aware of:

- The prompt is now version 5, with many more fields (see above). Images
  described by an older version keep their descriptions, which still show
  and search as before; run `scan.py --redescribe-stale` when you want them
  redone.
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

## Comparing models

    python compare_models.py --endpoints local,cloud --sample 100 --endpoint-limit cloud=20

Sends the same sample of your indexed images to several endpoints, one
endpoint at a time, and writes a side-by-side HTML report (titles,
descriptions, tags, text, types, flags, scores, time and tokens per image,
plus a summary per endpoint with images per minute and an estimated cost
per 10,000 images) and a JSON file with the raw results to `data/reports/`.
The database is only read; nothing is stored.

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
  `--sample`, then combine the results with
  `python compare_models.py --merge first.json second.json --out report.html`.

The reports contain thumbnails and text from your pictures; keep them
private.

## Searching

    python serve.py

This starts a local web server. Open the printed address in a browser. The
search page shows a grid of thumbnails that filters as you type. Click a
thumbnail to open its detail page with the full description, metadata, and
EXIF data; click the image there to view it at full size. With several
libraries you can search all of them or pick one.

## Managing libraries in the browser

The header of every page links to **Manage** and **Settings** when the
server runs on this computer (the default address, `127.0.0.1`). Everything
the command line does for libraries can be done there:

- **Manage** lists each library with its folder, whether the folder can be
  reached, how many pictures are described, waiting, out of date or failed,
  and the last job. Its buttons scan a library, describe it, do both, retry
  failed pictures, re-describe pictures described with an older prompt, and
  build missing previews. **Process all libraries** does a scan and describe
  of every library that is turned on.
- **Add library** asks for the folder (type it, or use **Browse...**; a
  network share is typed as `\\server\share`), suggests a name, shows how
  many pictures it found, and previews which files the ignore patterns skip.
  Editing a library can rename it or point it at a moved folder; its
  descriptions are kept. **Remove** asks you to type the name and whether to
  keep the indexed data (it can be attached to a library again later) or
  delete it.
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
  make one the default. **Test** describes the bundled sample picture and
  shows the answer, the time it took and, with prices set, the cost per
  picture. Before the first describe of a library that uses a cloud service,
  the page says how many pictures will be sent and where. A cloud service
  whose API key is not set up is never sent a picture: Manage says what is
  missing and keeps the describe buttons off.

These pages only answer requests from this computer. When the server is
started with `--host` so that other computers can search, Manage and
Settings are turned off. Do not put the server behind a reverse proxy on
the same computer: every visitor would then look local.

## Packaging a static site

    python serve.py package
    python serve.py package site_example

The first form builds the site in `data/exports/site/` inside the data folder.
The second builds it in the named folder instead, which must be new, empty,
or an earlier export: the command marks the folders it creates and refuses to
write into any other non-empty folder, because updating a site deletes stale
files in it. Use `--force` to update a site built by an older version, which
has no such mark. Inside the program folder only `site/`, `site_<name>/` and
`exports/` are accepted (git ignores those), so your photos never end up in a
commit. Re-running the command updates an existing package in place.

The result works without a server. Serve the folder with any static web
server, for example:

    python -m http.server --directory site_example

It looks and behaves like the live version, searching the data in the browser.
The search data is split into files no larger than 10 MB, and full-size images
are copied into the site's `images/` folder. Re-running `package` after a
small change only rewrites the files that changed.

## Files

- `settings.py` - manage settings
- `scan.py` - scan libraries, then request descriptions
- `describe.py`, `prompts.py` - the describe step and the prompt it sends
- `compare_models.py` - compare how endpoints describe the same images
- `serve.py` - search server and static site packager
- `admin.py`, `library_ops.py` - the Manage and Settings pages' server side
  (library changes, folder checks, jobs, endpoints)
- `static/`, `templates/` - the web pages (`shell.js` is the header shared
  by every page; `manage.js` and `settings.js` run Manage and Settings)
- `progress.py` - show how many images are described per library, how many
  have descriptions from an older prompt, and which failed
- `world_map.py` - render a world map with a pin for each GPS-tagged area
- `make_tiles.py` - download a detailed world image and slice it into map
  tiles (optional)
- `common.py`, `db.py`, `cache.py`, `jobs.py`, `images.py`, `imageinfo.py`,
  `llm.py` - shared helpers (settings, database, thumbnails and previews,
  job control, image reading, EXIF facts, LLM calls)
- `SETTINGS.md` - reference for every field in `config.json`
- `data/` - database, thumbnails, previews and the default static site
  export (`data/exports/site/`)

## Tests

    python -m unittest discover -s tests

`tests/test_repo_hygiene.py` checks that nothing personal (absolute paths,
host names, model files) is about to be committed. To add your own private
words to that check, list them one per line in `private-terms.txt` inside
your `SETTINGS_BASE` folder; that file is never committed.

## License

The code is released under the MIT License; see [LICENSE](LICENSE). The
sample images are covered by their own licenses, listed in
[sample/images/CREDITS.md](sample/images/CREDITS.md).

## Credits

`static/worldmap.jpg` is an equirectangular projection derived from NASA's
Blue Marble imagery and is in the public domain. Source:
https://commons.wikimedia.org/wiki/File:Equirectangular-projection.jpg
