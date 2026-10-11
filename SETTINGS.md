# Settings Reference

All configuration lives in a single JSON file named `config.json`. The
Manage and Settings pages of the web server (`python serve.py`, then the
links in the page header) and the `settings.py` script edit the most common
fields, but the file is just JSON and you can edit it by hand whenever you
need a setting they do not cover. The pages re-read the file before every
change, so hand edits are kept; a page that was open while you edited the
file asks you to reload it before it saves anything. The helper module path
can only be set in the file or with `settings.py`, never from a web page.

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
    "endpoints": [ { ... }, { ... } ],
    "default_endpoint": "local",
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

## endpoints

The LLM services that describe your images. `endpoints` is a list; each
entry has a unique `name`, and `default_endpoint` names the one libraries use
unless they pick another (see the library `endpoint` setting below). Without
a `default_endpoint`, the first enabled endpoint on this computer or the
local network is the default, and if there is none, libraries that name no
endpoint are not described: pictures never go to a cloud service nobody
chose for them. (`settings.py` sets it when you add the first endpoint.)

```
"endpoints": [
    {
        "name": "local",
        "preset": "llamacpp",
        "base_url": "http://127.0.0.1:8080/v1",
        "model": "your-vision-model"
    },
    {
        "name": "cloud",
        "preset": "anthropic",
        "model": "claude-haiku-5-5",
        "api_key_env": "ANTHROPIC_API_KEY"
    }
],
"default_endpoint": "local"
```

`settings.py` adds, edits and removes endpoints, and can test one: it sends
the sample picture and shows the reply, the time it took and the tokens
used. `python scan.py --list-endpoints` lists them and
`python scan.py --test-endpoint NAME` runs the same test.

An endpoint has these settings. Only `name`, `preset` (or `kind`) and
`model` are needed; everything else has a sensible default.

- `name` - a unique name (no commas or `=`), used by libraries, the command
  line and the provenance stored with each description.
- `preset` - the kind of service, which fills in the API to speak, the
  default base URL, the usual environment variable for the key and how many
  requests to send at once (see the table below). Left out, it is guessed:
  `kind` `claude` means `anthropic`, `ollama` means `ollama`, an `openai`
  endpoint without a base URL is `openai`, one on a cloud service's host
  gets that service's preset, and one on this computer or the local network
  is treated like llama.cpp.
- `kind` - the API itself: `openai` (chat completions, also spoken by most
  local servers), `claude` (Anthropic's Messages API) or `ollama` (Ollama's
  own API). The preset sets it; you only need it without a preset.
- `base_url` - the API address. Blank uses the preset's default. Typed
  without `http://` or `https://`, it gets `http://` for an address on this
  computer or the local network and `https://` for anything else; the cloud
  services' own addresses always use `https://`. A plain `http://` address
  outside the local network is not encrypted (the run log warns about it).
  Redirects are never followed (they would carry the key elsewhere): an
  address that redirects is reported, so you can enter the final one.
- `model` - the model to ask for. For llama.cpp, the server answers with
  whatever model it loaded; the name it reports is what gets recorded.
- `api_key_env` - the name of an environment variable holding the API key
  (recommended).
- `api_key_file` - a file that holds only the key (a relative path is next
  to `config.json`). Keep it outside the program folder.
- `api_key` - the key itself, stored in `config.json` (not recommended).
- `concurrency` - how many requests to keep in flight at once. By default,
  llama.cpp's slot count (its `--parallel` setting), 4 for cloud services and
  1 for other local servers.
- `max_image_edge` - the longest image edge sent, in pixels (default 1536).
  Tall screenshots keep a short side of at least 768 pixels so their text
  stays legible.
- `timeout` - seconds to wait for a reply (default 180).
- `max_output_tokens` - the longest reply (default 4000; 8000 for Claude
  models that always think, since thinking counts against it).
- `extra` - fields merged into every request, for anything the presets do
  not cover, for example `{"temperature": 0.2}` or
  `{"reasoning_effort": "low"}`. Nested objects are merged; a `null` value
  removes a field that would otherwise be sent.
- `price_in`, `price_out` - USD per million input and output tokens, used to
  estimate costs (left out: no estimates).
- `label` - a display name (default: the name).
- `enabled` - `false` keeps the endpoint in the settings without using it.
- `group` - endpoints with the same group name share the work when a
  library (or `default_endpoint`) names the group, for example two local
  machines running the same model.

The API key is looked up in this order: `api_key_env`, `api_key_file`,
`api_key`, and finally the preset's usual environment variable (for example
`OPENAI_API_KEY`) when none of the others is set -- that last one only when
the base URL is the preset's own service, so a gateway or proxy at another
address never gets it (name the variable in `api_key_env` if it should). A
key file that cannot be read is reported as such rather than replaced by
another key. Keys are never printed, logged or stored with the descriptions;
the tools only say where a key comes from ("env OPENAI_API_KEY", "file",
"config", "none"). Changing an endpoint's address (its base URL, or its
preset when it has no base URL of its own) in `settings.py` removes a key
typed into it.

| Preset | Service | Default base URL | Key variable |
|---|---|---|---|
| `llamacpp` | llama.cpp's `llama-server` | `http://127.0.0.1:8080/v1` | |
| `lmstudio` | LM Studio | `http://127.0.0.1:1234/v1` | |
| `vllm` | vLLM or SGLang | `http://127.0.0.1:8000/v1` | |
| `ollama` | Ollama's own API | `http://127.0.0.1:11434` | |
| `ollama-openai` | Ollama's OpenAI-compatible API | `http://127.0.0.1:11434/v1` | |
| `openai` | OpenAI | `https://api.openai.com/v1` | `OPENAI_API_KEY` |
| `azure` | Azure OpenAI (`https://RESOURCE.openai.azure.com/openai/v1`, model = deployment) | | `AZURE_OPENAI_API_KEY` |
| `gemini` | Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai` | `GEMINI_API_KEY` |
| `openrouter` | OpenRouter | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` |
| `anthropic` | Anthropic's Claude models | `https://api.anthropic.com` | `ANTHROPIC_API_KEY` |
| `custom` | any other OpenAI-compatible service | | |

What the presets take care of:

- Replies must follow a JSON schema. Each service gets the parts of the
  schema it accepts (cloud services reject some length limits, Claude also
  number and list limits); the limits are checked again when replies are
  read.
- llama.cpp and other local servers are asked to turn thinking off
  (`chat_template_kwargs`), OpenAI and Azure get `max_completion_tokens`
  instead of `max_tokens`, Ollama gets `think: false`, and Claude models get
  the least thinking each allows (`claude-haiku-5-5` none, `claude-sonnet-5-5`
  only between tools, `claude-opus-5-5` and `claude-fable-5-1` low effort).
  Use `extra` to change any of this.
- OpenRouter is asked to use only providers that honor the schema and do
  not store or train on your pictures (`"provider": {"require_parameters":
  true, "data_collection": "deny"}`). If no provider of a model qualifies,
  allow it with `"extra": {"provider": {"data_collection": "allow"}}`.
- Rate limits, busy servers, dropped connections and server errors are
  retried with growing pauses (honoring the server's `Retry-After` or "try
  again in ..."). A rejected key, no credit or a spending limit stops the
  run with a message. A setting the service rejects (an unknown model, say)
  or a daily quota stops using that endpoint for the run. A picture the
  service's content policy blocks fails like any unusable image. Problems of
  the endpoint are not charged to images, except that an image an endpoint
  that worked earlier in the run keeps failing on (or stops on) counts one
  failed attempt, so one picture cannot hold up every run; images that
  failed before are described after the others.

Requests carry only the prepared image and the prompt: no file name, folder
or EXIF data. With a cloud preset, your pictures are sent to that company.

### Older forms

Older files have a single `"endpoint": {...}` object, or a list of them
whose members share the work (round robin):

```
"endpoint": [
    {"kind": "openai", "base_url": "http://host-a:8080/", "model": "..."},
    {"kind": "openai", "base_url": "http://host-b:8080/", "model": "..."}
]
```

Both still work unchanged: they behave as endpoints named `default`,
`default-2`, ... (a list's members form the group `default`), which is what
`default_endpoint` means for such files. Editing endpoints in `settings.py`
saves them in the newer `endpoints` form, keeping those names.

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

The `call_api(prompt, image_path)` hook gets the text sent after the image
(the system prompt, with the instructions, is sent as it is) and a temporary
JPEG holding the image as it will be sent. Declare a third parameter,
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
  in the data folder, used by the detail page and for describing, and for
  larger 360-degree photos also a panorama up to 4096 px wide for the 360
  view. With `false` they are made from the original each time they are
  shown.
- `newest_first` - describe the most recently taken images first (default
  `true`).
- `endpoint` - the endpoint (or group) that describes this library's
  images; blank uses `default_endpoint`. A name that matches no enabled
  endpoint means the library's images are not described (they are never
  sent somewhere else instead).
- `metadata` - `"core"` (the default) or `"extended"`. Extended also asks
  for counted objects, activities, landmarks, a location guess and the mood,
  which takes longer and costs more tokens.
- `context_notes` - optional background sent with every image of the
  library, such as "Photos from a sailing club" or "Scans of family
  letters". It can help the descriptions, but it is sent to the endpoint's
  service, so do not put anything private here. Names of people in the
  notes are not used to identify anyone.
- `export` - optional saved options for publishing the library as a static
  website (see README, "Publishing a library"). The Export dialog on the
  Manage page writes them when an export starts (only the options that
  differ from the defaults are stored). Every option may be left out; the
  defaults publish as little as possible:
  - `title` - the site's name; blank (the default) uses the library name.
  - `images` - `"web"` (the default: 2048 px pictures), `"original"` (the
    originals with their metadata removed; HEIC, HEIF, AVIF and TIFF become
    JPEG, BMP becomes PNG) or `"none"` (thumbnails only).
  - `location` - `true` adds GPS positions (rounded to about 10 m), the map,
    and the AI model's separate landmark and place-guess fields (extended
    metadata); default `false`. Titles and descriptions are published as
    written either way, and can name a place a picture shows.
  - `file_names`, `folders` - `true` publishes file names or folder names
    (searchable); default `false`.
  - `exclude_types` - picture types left out; default
    `["screenshot", "document"]` (the others are `scan`, `artwork`, `meme`,
    `diagram`, `photo` and `other`). A picture whose document type is a
    receipt, letter, form, ticket or handwriting also counts as a
    `document`, and one showing a chat, web page or app as a `screenshot`,
    whatever its photo type.
  - `exclude_private`, `exclude_sensitive`, `exclude_undescribed` - leave
    out pictures flagged as showing private information (or an ID card),
    as suggestive, explicit or graphic, or not checked yet (not described,
    or described by the older version 4 prompt, which did not look for
    private or sensitive content); all `true` by default.
  - `include_ids` - pictures published although the rules above leave them
    out: `[id, ["flag", ...]]` publishes a picture while it has no flag that
    leaves it out other than those (the Export dialog stores the flags the
    picture had when you chose it, so a new flag leaves it out again); a
    plain id publishes it whatever its flags. Flags: `private`,
    `sensitive`, a picture type, `writing`, `unchecked`, `undescribed`.
  - `exclude_ids` - picture ids left out whatever the rules above say.
    Hidden pictures are never published.
  - `credits` - a line of plain text shown at the foot of the pages.
  - `zip` - `true` also writes `<folder>.zip` next to the output folder.
  - `allow_indexing` - `true` lets search engines index the pages
    (default `false`: the pages and a `robots.txt` ask them not to list
    the pages or their pictures; anyone with the address can still open
    them).

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
    "endpoints": [
        {
            "name": "local",
            "preset": "llamacpp",
            "base_url": "http://127.0.0.1:8080/v1",
            "model": "your-vision-model"
        },
        {
            "name": "cloud",
            "preset": "openai",
            "model": "your-cloud-model",
            "api_key_env": "OPENAI_API_KEY",
            "price_in": 0.1,
            "price_out": 0.4
        }
    ],
    "default_endpoint": "local",
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
            "path": "D:\\Photos\\Vacation",
            "endpoint": "cloud",
            "metadata": "extended",
            "context_notes": "Holiday photos: beaches, hiking and old towns."
        }
    ]
}
```
