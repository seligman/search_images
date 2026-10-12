# Settings Reference

All configuration lives in a single JSON file named `config.json`. The
Manage and Settings pages of the web server (`python serve.py`, then the
links in the page header) and the `settings.py` script edit the most common
fields, but the file is just JSON and you can edit it by hand whenever you
need a setting they do not cover. The pages re-read the file before every
change, so hand edits are kept; a page that was open while you edited the
file asks you to reload it before it saves anything. `settings.py` checks
too: if the file changed while it was open, **Save and exit** asks whether
to save over those changes, reload the file or exit without saving. The
helper module path can only be set in the file or with `settings.py`, never
from a web page, and so can the program and command line of the model
server (`local_server`).

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
    "local_server": { ... },
    "nadir_fill_deg": 0,
    "zenith_fill_deg": 0,
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
  gets that service's preset, one on this computer or the local network
  gets `local` (treated like llama.cpp), and anything else gets `custom`.
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
  the server's slot count for `llamacpp` and `local` (llama.cpp's
  `--parallel` setting), 4 for the cloud services and 1 for the others.
- `max_image_edge` - the longest image edge sent, in pixels (default 1536).
  Tall screenshots keep a short side of at least 768 pixels so their text
  stays legible.
- `timeout` - seconds to wait for a reply (default 180).
- `max_output_tokens` - the longest reply (default 4000; 8000 for Claude
  models that always think, since thinking counts against it). A reply cut
  off at this limit is kept when it got as far as the title and the
  description (the rest of it is left out; its provenance says `truncated`
  and lists the `missing_fields`); otherwise the picture is asked for again
  with twice the limit (once; when the service refuses that many tokens,
  the picture's other tries use this limit). A reply whose transcribed
  text loops until it is cut off is asked for again first, and kept only
  when the retries do no better. A cut-off reply that did not reach
  `private_info` is stored as showing private information.
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
typed into it. **List models** on the Settings page, for an endpoint not
saved yet (or with unsaved changes), only uses a key from `api_key_env` or
`api_key_file` when a saved endpoint sends that same key to the same
address, or when the address is the preset's own service.

| Preset | Service | Default base URL | Key variable |
|---|---|---|---|
| `llamacpp` | llama.cpp's `llama-server` | `http://127.0.0.1:8080/v1` | |
| `local` | any OpenAI-compatible server on this computer or the local network, treated like llama.cpp | `http://127.0.0.1:8080/v1` | |
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

Optional path to a Python file that wraps the LLM calls and the file
access. See `example_helper.py` for the available hooks. Leave blank if you
do not need one. It can, for instance, start the model server before
describing and stop it afterwards.

```
"helper": "my_helper.py"
```

A relative path is relative to the folder holding `config.json`. (Older
versions used the current directory; a helper found only there, or next to
the scripts, is still loaded, with a warning.)

Which tools use which hooks:

- `before_launch()` and `after_launch()` run around every describe run
  that has pictures to describe: the describe step of `scan.py`, and the
  Describe, Scan + describe, Process all and Re-describe jobs of the Manage
  page (and "Describe again" in the viewer). Jobs run inside the web
  server's process, so a server started there belongs to `serve.py`. A
  local server that `before_launch` started is waited for (up to three
  minutes) while it loads its model.
- `call_api()` runs before each request of a describe run.
- The endpoint tests (**Test** on the Settings page, the test in
  `settings.py`, `scan.py --test-endpoint`) and `compare_models.py` use none
  of these three: start the model server yourself before using them.
- `walk_library()` lists a library's files for scans (and the Build
  previews job). `load_image()` and `stat()` are used wherever originals
  are read: scans, describing, the search server (originals, and previews
  it makes on the fly) and exports.

The `call_api(prompt, image_path)` hook gets the text sent after the image
(the system prompt, with the instructions, is sent as it is) and a temporary
JPEG holding the image as it will be sent. Declare a third parameter,
`call_api(prompt, image_path, source_path=None)`, to also get the path of the
original file. The `stat(path)` hook, when present, supplies the size and
time of every image the scanner lists.

## local_server

Optional: lets the Settings page (and describing) start and stop a local
model server, llama.cpp's `llama-server`, for one of the endpoints. Without
it, start the server yourself.

```
"local_server": {
    "enabled": true,
    "endpoint": "local",
    "binary": "C:\\path\\to\\llama-server.exe",
    "models_dir": "C:\\path\\to\\models",
    "model": "model.gguf",
    "port": 8080,
    "slots": 4,
    "ctx_per_slot": 8192,
    "stop_at": "03:30"
}
```

- `enabled` - `true` turns it on (default `false`).
- `endpoint` - the endpoint the server answers for. Its base URL must be
  `http://127.0.0.1:<port>/v1` (or `localhost`); the Settings page says so
  when it is not, and moves it along when you change the port there (an
  API key typed for the endpoint is then removed, as for any change of
  its address).
- `models_dir` - the folder with the model files (`.gguf`); a relative path
  is next to `config.json`. The Settings page offers the files in it and
  in its sub-folders (one level down) and nothing else.
- `model` - the model file, relative to `models_dir`.
- `mmproj` - the vision projector, relative to `models_dir`; `""` (the
  default) finds it: the file named like the model with `mmproj` added
  before the extension (`model.mmproj.gguf` for `model.gguf`), or, for a
  model in a sub-folder, the file there with `mmproj` in its name. After
  starting, the server must report that it can see images, or it is
  stopped again.
- `binary` - the `llama-server` program (a full path, a path relative to
  `config.json`, or a name found on the PATH).
- `port` (default 8091), `slots` (requests at the same time, `-np`; default
  4) and `ctx_per_slot` (context per slot, default 8192; the server gets
  `-c slots * ctx_per_slot`).
- `extra_args` - more arguments for `llama-server`, such as
  `["--threads", "8"]`.
- `launch_command` - a command line of your own that replaces the
  program and its arguments, as a list, for a wrapper script (one that
  takes a lock first, say). These words in it are replaced: `{model_path}`,
  `{mmproj_path}` (the full paths; `""` without a projector), `{port}`,
  `{slots}`, `{ctx}` (slots times context), `{ctx_per_slot}`, `{stop_at}`
  (the `stop_at` time, `""` for none) and `{python}` (the Python that runs
  the server). `extra_args` is not added to it. The wrapper should stop its
  own `llama-server` when it gets Ctrl+Break (Windows) or SIGTERM; if it
  ends without doing so, the server it started is still stopped (see
  below).
- `auto_start` - a Describe, Scan + describe or Refine job (and `scan.py`)
  that uses this endpoint starts the server when it is not running, and
  waits for it to load the model (default `true`).
- `auto_stop` - such a job stops the server again when it has finished, if
  it started it itself (default `true`). A server started on the Settings
  page keeps running.
- `stop_at` - `"HH:MM"` (24-hour clock, local time), or `""` for none:
  every day the web server stops the server at that time, if these tools
  started it before that time (it keeps trying for an hour if the stop
  fails; a server started with **Start** after the stop time keeps
  running, also when the web server is restarted). Jobs that use this
  endpoint stop ten minutes before (they are listed as interrupted and can
  be resumed), and from then until an hour after that time no job uses it
  or starts it; a job started then fails with a message that says until
  when. **Start** on the Settings page still works in that hour.
- `start_timeout` - seconds to wait for the server to load its model
  (default 900); after that it is stopped again.

The server starts in its own process group, listening on `127.0.0.1` only,
with `-fa on --reasoning off --no-webui`; its output goes to
`data/logs/model-server.log`. **Stop** asks it to stop (Ctrl+Break on
Windows, SIGTERM elsewhere) and, if it is still running after 20 seconds,
ends it and every process it started. Only a server started by these tools
is ever stopped (its process id and start time are kept in
`data/model-server.json`, so a restarted web server can still stop it); a
server that already answers on the port is used as it is (also while it
still loads its model) and left alone. The processes the server started
count as part of it, even when a wrapper in `launch_command` has ended
before them (on Windows: the processes whose parent it was, started after
it; elsewhere: its process group). If its port still answers after the
stop, the panel says so instead of "stopped". The web server, its jobs and
`scan.py` share `data/model-server.json`, and change it only while they
hold `data/model-server.lock`.

The Settings page may change `model`, `mmproj`, `slots`, `ctx_per_slot`,
`port`, `stop_at`, `auto_start` and `auto_stop`. The other settings
(`enabled`, `endpoint`, `binary`, `models_dir`, `extra_args`,
`launch_command`, `start_timeout`) are only changed here, in
`config.json`: a web page can never choose which program runs.

`local_server` may also be a list of such objects, one per endpoint (for
example a small model for describing and a larger one for the refine
pass); each then logs to `data/logs/model-server-<endpoint>.log`. Most
graphics cards hold one model at a time: a job that needs the other
server waits for it to start, so stop the first one (auto_stop does this
after each job).

## reader_threads

How many images the scanner reads and decodes at the same time (1 to 32,
default 6). More threads help with slow network shares; fewer use less
memory.

## purge_after_days

A file that disappears from a library is marked missing rather than removed,
so its description (and your own edits of it) comes back if the file
returns. After this many days (default 30) missing files are removed from
the database, with your edits of them (a copy of the picture that is still
in a library keeps the edits). Use a negative number to never remove
them.

## nadir_fill_deg, zenith_fill_deg

Unstitched 360-degree photos (Insta360 `.insp` frames) are stitched into a
panorama here. Right below the camera there may be a tripod's legs, or the
hand that held it. `nadir_fill_deg` covers that many degrees around the
bottom of the panorama with a soft fill: the colours around it, spread
smoothly towards the middle, with no hard edge (it also fades in over 3
more degrees). It is off by default (0), because on clean ground the fill
only leaves a grey disc where the ground was. Turn it on, for example with
6 (about what a tripod's legs or a hand take), if your 360 photos show a
tripod or a hand at the bottom. A hand-held selfie stick lies between the
two lenses, where the stitch hides it anyway. `zenith_fill_deg` does the
same at the top (default 0: none; set it when the camera hung from
something). At most 30. Panoramas the camera stitched itself, and other
360 photos, are shown as they are.

After a change, the next scan stitches the 360-degree photos in the index
again (their previews, panoramas and thumbnails; descriptions are kept).

## libraries

A list of folders to scan. Each library is one object:

- `id` - assigned automatically the first time the tools see the library
  (for example `lib_1a2b3c4d`) and never changed afterwards. The indexed
  images belong to the id, so a library can be renamed freely. Do not edit
  or copy it.
- `name` - a short label shown in the search UI. Must be unique (ignoring
  case).
- `path` - the absolute folder to scan (`~` is expanded), such as
  `C:\Photos\Family`, `\\server\share\Photos` or `/home/you/Pictures`
  (in the JSON file each backslash is doubled, as in the examples below).
  All image files found anywhere under this folder are added to the
  library, with the folder structure preserved as part of the search text.
  Symbolic links and directory junctions inside it are not followed. The
  pages and `settings.py` refuse a folder inside the program folder or the
  data folder (copy pictures from there to a folder of their own).
  `settings.py` also takes a relative path, which is then relative to the
  folder the tools are run from; the pages need a full path.
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
  view. They take about 0.5 MB per picture (about 50 GB per 100,000). With
  `false` they are made from the original each time they are shown.
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
- `refine` - optional: a second endpoint that describes the library's
  documents and screenshots again (the refine pass; see README, "Refining
  documents and screenshots"). `{}` or no `endpoint` turns it off (the
  default):
  - `endpoint` - the endpoint (or group) that refines.
  - `photo_types` - the kinds of picture refined; default
    `["screenshot", "document", "scan"]` (the others are `photo`,
    `artwork`, `meme` and `diagram`).
  - `document_types` - and the document types refined whatever the kind of
    picture; default `["receipt", "letter", "form", "card_or_id", "ticket",
    "chat", "web", "app", "handwriting", "book_page", "slide"]` (the others
    are `whiteboard`, `sign`, `menu` and `map`).
  - `max_output_tokens` - the longest answer, in tokens (default 8000; at
    least 500). It is used for the refine pass when it is larger than the
    endpoint's own `max_output_tokens`.

  The refine pass always asks for the standard (core) fields. Hidden
  pictures are only refined by an endpoint on this computer or the local
  network (with only cloud refine endpoints, the Manage page leaves them
  out of the count). A refine endpoint for a cloud service needs its API
  key before the pass can start. A cut-off refine answer keeps the earlier
  description's values for the fields it did not reach, and never clears
  its private or sensitive flags.
- `export` - optional saved options for publishing the library as a static
  website (see README, "Publishing a library"). The Export dialog on the
  Manage page writes them when an export starts (only the options that
  differ from the defaults are stored). Every option may be left out; the
  defaults publish as little as possible:
  - `title` - the site's name; blank (the default) uses the library name.
  - `images` - `"web"` (the default: 2048 px pictures), `"original"` (the
    originals with their metadata removed; HEIC, HEIF, AVIF and TIFF become
    JPEG, BMP becomes PNG, and an unstitched dual-fisheye 360-degree photo
    becomes its stitched panorama) or `"none"` (thumbnails only).
  - `location` - `true` adds GPS positions (rounded to about 10 m), the map,
    the place names (town, region and country, when they were downloaded:
    see "Place names" in the README; the pages then credit GeoNames at their
    foot, the map in a corner) and the AI model's separate landmark and place-guess fields
    (extended metadata); default `false`. Titles and descriptions are
    published as written either way (with your own edits to the title,
    description and tags in place of the model's), and can name a place a
    picture shows.
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
    An existing file of that name is only replaced when an earlier export
    made it and it was not changed since; otherwise the export stops.
  - `allow_indexing` - `true` lets search engines index the pages
    (default `false`: the pages and a `robots.txt` ask them not to list
    the pages or their pictures; anyone with the address can still open
    them).

A library removed from the file is not deleted from the database: its images
stay there, unused, until you delete them or attach them to a library again
(the Manage page shows such data in a notice, with **Re-attach to...** and
**Delete...**).

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

A `*` matches any characters, `/` included, so a pattern that starts with
`*` matches at any depth, while one that starts with a name is anchored at
the library root. A pattern that matches a folder skips the folder and
everything in it. Matching ignores case on Windows only. A few more
examples:

- `Private/*` - skip everything under the top-level `Private` folder.
- `Originals` - skip the file or folder named `Originals` at the root, and
  everything in it (`x/Originals` is kept).
- `*.tmp` - skip `.tmp` files anywhere in the library.
- `*/cache/*` - skip every `cache` folder below the root, at any depth (add
  `cache` as well to skip one at the root too).
- `IMG_*.png` - skip matching files at the root only; `*/IMG_*.png` skips
  them in subfolders.

The **Add library** and edit dialogs on the Manage page preview which files
the patterns skip, so you can check a pattern before saving it.

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

With a model server the Settings page starts and stops, and a stronger
model for the documents of the Family library (`local_server` and
`refine`, both optional):

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
            "name": "large",
            "preset": "llamacpp",
            "base_url": "http://127.0.0.1:8081/v1",
            "model": "your-larger-model"
        }
    ],
    "default_endpoint": "local",
    "local_server": [
        {
            "enabled": true,
            "endpoint": "local",
            "binary": "C:\\path\\to\\llama-server.exe",
            "models_dir": "C:\\path\\to\\models",
            "model": "model.gguf",
            "port": 8080,
            "stop_at": "03:30"
        },
        {
            "enabled": true,
            "endpoint": "large",
            "binary": "C:\\path\\to\\llama-server.exe",
            "models_dir": "C:\\path\\to\\models",
            "model": "your-model.gguf",
            "port": 8081,
            "slots": 2,
            "ctx_per_slot": 16384
        }
    ],
    "libraries": [
        {
            "name": "Family",
            "path": "C:\\Photos\\Family",
            "refine": {"endpoint": "large"}
        }
    ]
}
```
