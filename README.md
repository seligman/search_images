# Image Database

A searchable database of your images. It scans folders of pictures, asks an
LLM to describe each one (including any text in the image), and lets you search
those descriptions from a web page.  You can see a [demo of the site this produces here](https://seligman.github.io/search_image_sample/index.html).

Supported image types are JPG/JPEG, PNG, GIF, WebP, TIFF, BMP, HEIC/HEIF and
AVIF (HEIC needs `pillow-heif`; AVIF needs a Pillow built with AVIF support).

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

Run the settings tool and follow the numbered menu:

    python settings.py

From there you can:

- Choose the LLM endpoint: OpenAI, Claude, or a local server (Ollama, or
  llama.cpp using the `openai` kind with its base URL).
- Set an optional helper module that wraps the LLM calls. See
  `example_helper.py`.
- Add the image libraries to monitor. Each library has a name and a folder.

Settings are stored in `config.json`. See [SETTINGS.md](SETTINGS.md) for the
full format, including options the menu does not edit (such as per-library
ignore patterns and round-robin endpoints).

## Scanning

    python scan.py

This finds new and changed images and reads each of them once: it stores
their metadata, EXIF data, capture date, camera and GPS position in a SQLite
database (`data/index.db`), builds a thumbnail and a 2048 px preview, and then
asks the LLM to describe anything without a description. The LLM replies with
a JSON structure (description, subjects, visible text, tags, and colors) that
is parsed and stored. Run it again whenever your folders change; unchanged
files are not read again.

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
- `--verbose` lists every added or changed file (by default only the first
  20 of each kind per library, then a count).

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

## Searching

    python serve.py

This starts a local web server. Open the printed address in a browser. The
search page shows a grid of thumbnails that filters as you type. Click a
thumbnail to open its detail page with the full description, metadata, and
EXIF data; click the image there to view it at full size. With several
libraries you can search all of them or pick one.

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
- `serve.py` - search server and static site packager
- `progress.py` - show how many images are described, per library
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
