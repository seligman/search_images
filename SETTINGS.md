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
    "libraries": [ { ... }, { ... } ]
}
```

The three top-level keys are described below. Anything else in the file is
ignored, so it is safe to keep your own notes or extra fields alongside the
real settings.

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
    "model": "Gemma 3, 27B"
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
"helper": "config_helper.py"
```

## libraries

A list of folders to scan. Each library is one object:

- `name` - a short label shown in the search UI. Must be unique.
- `path` - the absolute folder to scan. All image files found anywhere under
  this folder are added to the library, with the folder structure preserved
  as part of the search text.
- `ignore` - optional list of wildcard patterns. Any file whose path,
  relative to the library root, matches one of these patterns is skipped.
  See "Ignore patterns" below.

Minimal example:

```
"libraries": [
    {
        "name": "Image Library",
        "path": "C:\\Users\\scott\\Pictures\\Image Library"
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
    "name": "Image Library",
    "path": "C:\\Path\\Image Library",
    "ignore": [
        "Documents/*"
    ]
}
```

```
{
    "name": "Image Library",
    "path": "C:\\Path\\Image Library",
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
is removed from the database on the next full scan, the same as if it had
been deleted from disk.

## Full example

```
{
    "endpoint": {
        "kind": "openai",
        "base_url": "http://localhost:8080/",
        "api_key": "",
        "model": "Gemma 3, 27B"
    },
    "helper": "config_helper.py",
    "libraries": [
        {
            "name": "Image Library",
            "path": "C:\\Users\\scott\\Pictures\\Image Library",
            "ignore": [
                "Documents/*",
                "*.tmp"
            ]
        },
        {
            "name": "Vegas Trip",
            "path": "C:\\Users\\scott\\Pictures\\Vegas Trip"
        }
    ]
}
```
