# Settings Reference

All configuration lives in a single JSON file named `config.json`. The
`settings.py` script is a convenience wrapper that edits the most common
fields, but the file is just JSON and you can edit it by hand whenever you
need a setting that the menu does not cover.

## Where the file lives

By default `config.json` and the `data/` folder sit next to the scripts. Set
the `SETTINGS_BASE` environment variable to keep them somewhere else, for instance
on Linux or macOS:

    SETTINGS_BASE=/path/to/store python scan.py

Or, on Windows:

    set SETTINGS_BASE=c:\path\to\data
    python scan.py

The same variable is honored by every script.

## Top-level shape

```
{
    "endpoint": { ... },
    "helper":   "path/to/helper.py",
    "reader_threads": 6,
    "purge_after_days": 30,
    "libraries": [ { ... }, { ... } ]
}
```

The keys are described below. Anything else in the file is ignored, so it is
safe to keep your own notes or extra fields alongside the real settings. The
tools fill in defaults for keys that are missing and may write them into the
file (for example when a new library gets its `id`).

The file is read as UTF-8 (a file saved in the system's encoding by an older
editor still loads, with a warning). The tools write it with non-ASCII
characters escaped, such as `"Z\u00fcrich"`, which every version reads
correctly; you can type such names directly when editing by hand.

## endpoint

Describes the LLM that `scan.py` asks for image descriptions. It is either a
single object or a list of objects (round robin, see below). Each object has
these fields:

- `kind` - one of `openai`, `claude`, or `ollama`. Use `openai` for a local
  llama.cpp server as well, and point `base_url` at it.
- `model` - the model name to request. Leave blank to skip the LLM step;
  `scan.py` will index images without describing them.
- `base_url` - the API base URL. Blank uses the service default for `openai`
  and `claude`; a local server needs an explicit URL.
- `api_key` - the API key, if the endpoint needs one. Local servers usually
  do not.

Example:

```
"endpoint": {
    "kind": "openai",
    "base_url": "http://localhost:8080/",
    "api_key": "",
    "model": "your-vision-model"
}
```

### Multiple endpoints (round robin)

Set `endpoint` to a list of objects to spread description work across several
LLM servers at once. Each entry has the same fields as the single form. The
scanner sends one image at a time to each entry in parallel, which is useful
when you have more than one local machine running an LLM:

```
"endpoint": [
    {"kind": "openai", "base_url": "http://host-a:8080/", "model": "..."},
    {"kind": "openai", "base_url": "http://host-b:8080/", "model": "..."}
]
```

Only entries that name a `model` are used.

## helper

Optional path to a Python file that wraps the LLM calls. See
`example_helper.py` for the available hooks. Leave blank if you do not need
one.  Can be useful, for instance, to start and stop the endpoint automatically.

```
"helper": "my_helper.py"
```

A relative path is relative to the folder holding `config.json`. (Older
versions used the current directory; a helper found only there, or next to
the scripts, is still loaded, with a warning.)

The `call_api(prompt, image_path)` hook gets a temporary JPEG holding the
image as it will be sent. Declare a third parameter,
`call_api(prompt, image_path, source_path=None)`, to also get the path of the
original file. The `stat(path)` hook, when present, supplies the size and
time of every image the scanner lists.

## reader_threads

How many images the scanner reads and decodes at the same time (1 to 32,
default 6). More threads help with slow network shares; fewer use less
memory.

## purge_after_days

A file that disappears from a library is marked missing rather than removed,
so its description comes back if the file returns. After this many days
(default 30) missing files are removed from the database. Use a negative
number to never remove them.

## libraries

A list of folders to scan. Each library is one object:

- `id` - assigned automatically the first time the tools see the library
  (for example `lib_1a2b3c4d`) and never changed afterwards. The indexed
  images belong to the id, so a library can be renamed freely. Do not edit
  or copy it.
- `name` - a short label shown in the search UI. Must be unique (ignoring
  case).
- `path` - the absolute folder to scan (`~` is expanded). All image files
  found anywhere under this folder are added to the library, with the folder
  structure preserved as part of the search text. Symbolic links and
  directory junctions inside it are not followed.
- `ignore` - optional list of wildcard patterns. Any file whose path,
  relative to the library root, matches one of these patterns is skipped.
  See "Ignore patterns" below.
- `default_ignores` - `true` (the default) also skips the junk that NAS boxes
  and operating systems leave in photo folders: `@eaDir`, `.@__thumb`,
  `#recycle`, `$RECYCLE.BIN`, `.Trashes`, `.thumbnails`, `._*` files and
  `Thumbs.db`, at any depth.
- `enabled` - `false` stops scanning the library and hides it from search;
  its indexed images are kept.
- `previews` - `true` (the default) keeps a 2048 px preview of every image
  in the data folder, used by the detail page and for describing.
- `newest_first` - describe the most recently taken images first (default
  `true`).

A library removed from the file is not deleted from the database: its images
stay there, unused, until you delete them or attach them to a library again.

Minimal example:

```
"libraries": [
    {
        "name": "Family",
        "path": "C:\\Photos\\Family"
    }
]
```

### Ignore patterns

`ignore` accepts shell-style wildcards (`*`, `?`, character classes). Paths
are matched against the path relative to the library's `path`, so a pattern
like `Documents/*` ignores every file and subfolder under a folder named
`Documents` directly under the library root.

Either path separator works in a pattern. These two entries behave the same
on every platform:

```
{
    "name": "Family",
    "path": "C:\\Photos\\Family",
    "ignore": [
        "Documents/*"
    ]
}
```

```
{
    "name": "Family",
    "path": "C:\\Photos\\Family",
    "ignore": [
        "Documents\\*"
    ]
}
```

A few more examples:

- `Private/*` - skip everything under the top-level `Private` folder.
- `*.tmp` - skip files named like `something.tmp` at the library root.
- `*/cache/*` - skip a `cache` folder one level deep, regardless of its
  parent.
- `Originals` - skip a single file or folder named exactly `Originals` at
  the root. Use `Originals/*` if you want to skip its contents.

A file that was scanned previously and is now covered by an `ignore` pattern
is marked missing on the next full scan (and removed after
`purge_after_days`), the same as a file deleted from disk. Such files do not
count towards the "most of the library vanished" safety check.

## Full example

```
{
    "endpoint": {
        "kind": "openai",
        "base_url": "http://localhost:8080/",
        "api_key": "",
        "model": "your-vision-model"
    },
    "helper": "my_helper.py",
    "libraries": [
        {
            "name": "Family",
            "path": "C:\\Photos\\Family",
            "ignore": [
                "Documents/*",
                "*.tmp"
            ]
        },
        {
            "name": "Vacation",
            "path": "D:\\Photos\\Vacation"
        }
    ]
}
```
