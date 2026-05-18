# Image Database

A searchable database of your images. It scans folders of pictures, asks an
LLM to describe each one (including any text in the image), and lets you search
those descriptions from a web page.  You can see a [demo of the site this produces here](https://seligman.github.io/search_image_sample/index.html).

Supported image types are JPG/JPEG, PNG, GIF, and HEIC.

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

Settings are stored in `config.json`.

## Scanning

    python scan.py

This finds new and changed images, stores their metadata and EXIF data in a
SQLite database, builds thumbnails, removes images that no longer exist on
disk, and asks the LLM to describe anything without a description. The LLM
replies with a JSON structure (description, subjects, visible text, tags, and
colors) that is parsed and stored. Run it again whenever your folders change.
Use `--no-describe` to index without calling the LLM.

To stop a long scan early, create a file named `abort` or `abort.txt` in the
directory you ran `scan.py` from. It finishes the current image, saves its
progress, and exits. Delete that file and run `scan.py` again to pick up where
it stopped.

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

The first form builds the site in the default `site/` folder. The second
builds it in the named folder instead. Either way, re-running the command
updates an existing package in that folder in place.

The result works without a server. Serve the folder with any static web
server, for example:

    python -m http.server --directory site

It looks and behaves like the live version, searching the data in the browser.
The search data is split into files no larger than 10 MB, and full-size images
are copied into the site's `images/` folder. Re-running `package` after a
small change only rewrites the files that changed.

## Files

- `settings.py` - manage settings
- `scan.py` - scan libraries and request descriptions
- `serve.py` - search server and static site packager
- `common.py`, `llm.py`, `images.py` - shared helpers
- `data/` - database and thumbnail files
- `site/` - the packaged static site
