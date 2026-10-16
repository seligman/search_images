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
server (`local_server`); People's settings (`people`) are set in the file
only.

## Where the file lives

By default `config.json` and the `data/` folder sit next to the scripts. Set
the `SETTINGS_BASE` environment variable to keep them somewhere else, for instance
on Linux or macOS:

    SETTINGS_BASE=/path/to/store python scan.py

Or, on Windows:

    set SETTINGS_BASE=c:\path\to\data
    python scan.py

The same variable is honored by every script, and the tools that jobs
have command lines for (scan.py, export.py, people.py, make_places.py,
trips.py, serve.py) also take the folder as `--settings-base FOLDER`. When the folder
is not the program folder, the command line a job logs (see "Automating"
in the README) ends with `--settings-base FOLDER`, so it runs as it is.

## Top-level shape

```
{
    "endpoints": [ { ... }, { ... } ],
    "default_endpoint": "local",
    "helper":   "path/to/helper.py",
    "reader_threads": 6,
    "purge_after_days": 30,
    "local_server": { ... },
    "embedding": { ... },
    "image_embedding": { ... },
    "nadir_fill_deg": 0,
    "zenith_fill_deg": 0,
    "level_horizon": true,
    "ffmpeg": "",
    "video_frames": 4,
    "video_sheet_check": true,
    "video_proxies": { "enabled": false, "height": 720, "crf": 26 },
    "drives": { "slack": 20, "min_clips": 3 },
    "drive_previews": { "enabled": false, "speed": 60, "height": 720 },
    "gps_check": { "enabled": true },
    "plain_language": true,
    "place_estimates": { "enabled": true },
    "gps_tracks": { "enabled": true },
    "trips": { "enabled": true },
    "trip_stories": { "endpoint": "" },
    "people": { "enabled": false },
    "local_times": { "enabled": true },
    "raw": { "enabled": true, "index_alone": true },
    "street_maps": { "radius_km": 20 },
    "named_places": { "enabled": true },
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
  stopped again. An embeddings server (below) has one only when it is set:
  then it runs an image embedding model for looks.
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
- `kind` - `"describe"` (the default: a vision model that describes
  pictures) or `"embeddings"`: an embedding model for search by meaning
  (see [embedding](#embedding)). An embeddings server is started with
  `--embeddings` (it pools as the model file says: mean for mdbr-leaf-ir,
  CLS for snowflake-arctic-embed, the last token for Qwen3-Embedding) and
  `ctx_per_slot` as its context and batch sizes (`-c`, `-ub`, `-b`: the
  longest text it can take at once), or the model's own context when that
  is smaller (512 for small BERT models), without a projector or slots,
  and counts as ready once it answers `/health`. A small model needs no
  graphics card: `"extra_args": ["--device", "none", "-t", "4"]` keeps it
  on the CPU (four threads), with CPU and CUDA builds of llama.cpp alike.
  **Update meaning search** and the embed step after scans, describe and
  refine jobs start it as describe jobs do (`auto_start`, `auto_stop`);
  the search page starts it too when a typed search needs it (with
  `auto_start` on; while it loads, searches go by the words alone). When
  such a start fails, searches do not try again for five minutes
  (**Start** on the Settings page, or a job, still does). Searches never
  start a server of the other kind.
  An embeddings server with `mmproj` set runs an image embedding model
  for looks (see [image_embedding](#image_embedding)): its command also
  gets `--mmproj <projector> -fa off`. Its `mmproj` is set in
  `config.json` only (the Settings page does not change it). Flash attention changes the vectors
  of pictures on a graphics card, so it stays off for image embeddings
  (a `launch_command` must pass `-fa off` too). **Update looks** starts it
  like describe jobs do, and the search page when a picture is dropped on
  it (`auto_start`; `idle_stop_minutes` stops it again).
- `idle_stop_minutes` - for a server the search page started for its
  searches: stop it when no search has used it for this many minutes
  (default 0: keep it running; it is also stopped when the web server
  stops, with `auto_stop` on, and at `stop_at`). A job using it keeps it
  running, and so does a `scan.py` run that is embedding with it. A small
  embedding model on the CPU takes about 50 MB of memory and a few
  milliseconds a search, so 0 suits it; a larger model, or one on the
  graphics card, can be stopped after, say, 30 idle minutes.

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
`port`, `stop_at`, `auto_start`, `auto_stop` and `idle_stop_minutes`. The
other settings (`enabled`, `kind`, `endpoint`, `binary`, `models_dir`,
`extra_args`, `launch_command`, `start_timeout`) are only changed here, in
`config.json`: a web page can never choose which program runs. Its Model
server panel marks an embeddings server "Embeddings" and offers no
projector or slots for it.

`local_server` may also be a list of such objects, one per endpoint (for
example a small model for describing and a larger one for the refine
pass); each then logs to `data/logs/model-server-<endpoint>.log`. Most
graphics cards hold one model at a time: a job that needs the other
server waits for it to start, so stop the first one (auto_stop does this
after each job).

## embedding

Optional: search by meaning (see "Search by meaning" in the README). An
embedding model turns the description of every described picture into a
meaning vector, and the search page turns what is typed into one too, to
find pictures that mean the same without sharing its words. "More like
this" ranks pictures by how close their vectors are.

```
"embedding": {
    "endpoint": "embed",
    "model": "",
    "dims": 256,
    "query_prefix": "Represent this sentence for searching relevant passages: ",
    "batch": 32,
    "allow_cloud": false,
    "model_file": "",
    "dense_file": "C:\\path\\to\\models\\model.dense.safetensors",
    "license_file": "C:\\path\\to\\models\\model.LICENSE.txt",
    "credit": "mdbr-leaf-ir by MongoDB (Apache-2.0)"
}
```

The recommended model is MongoDB's mdbr-leaf-ir (23 million parameters,
Apache-2.0): it runs on a CPU (the whole library in a few minutes, a few
milliseconds a search), keeps its meaning when cut to 256 numbers, finds
the pictures you mean at least as well as much larger models, and is small
enough to go along with a published site, so that typed searches find
pictures by meaning there too (the export's `meaning` option, see
[libraries](#libraries)). Download three files and keep them together:
the model as a Q8_0 GGUF file (a conversion of `MongoDB/mdbr-leaf-ir`; F16
and F32 ones work too, but take longer to convert for a published site),
its Dense head (the `2_Dense/model.safetensors` file of the model's
repository; llama.cpp leaves this last layer out, so these tools apply it
themselves, see `dense_file`), and its license text (Apache-2.0, with a
line saying where the model comes from). snowflake-arctic-embed-xs is
the other small model that works in a browser (it has no Dense head, but
cannot be cut below its 384 numbers: set `"dims": 0`).

- `endpoint` - the name of an endpoint (in `endpoints`) that serves an
  embedding model through an OpenAI-compatible `/v1/embeddings` API
  (llama.cpp's `llama-server --embeddings`, Ollama, LM Studio, vLLM,
  OpenAI) or Ollama's own `/api/embed` (the `ollama` preset); `""` (the
  default) turns search by meaning off. Claude endpoints cannot make
  embeddings. For llama.cpp:

      {"name": "embed", "preset": "llamacpp", "base_url": "http://127.0.0.1:8099/v1"}

  started for example as `llama-server -m model.gguf --embeddings --host
  127.0.0.1 --port 8099 -c 512 -ub 512 -b 512 --device none -t 4
  --cache-ram 0` (a BERT model such as mdbr-leaf-ir takes 512 tokens at
  most; Qwen3-Embedding 0.6B takes 8192), or by a `local_server` entry
  with `"kind": "embeddings"`. `--device none` keeps it on the CPU, which
  a small model is fast enough on. `--cache-ram 0` turns off the prompt
  cache of recent llama.cpp builds (up to 8 GB of memory by default),
  which embedding never reuses: every text is different. An older build
  that does not know the flag works without it; for a `local_server`
  entry, add `"extra_args": ["--cache-ram", "0"]`.
- `model` - the model name sent with each request; `""` sends the
  endpoint's `model` (llama.cpp needs none). The vectors are tied to this
  name (or, without one, to the endpoint's model or name), and to the
  model's name and vector length as its server reports them: when
  another model is loaded behind the same endpoint, the next embed run
  notices and makes every vector again (until then, typed searches go by
  the words).
- `dims` - keep this many numbers of each vector (the first ones) and
  scale them back to unit length: 256 by default, 0 for all. Models
  trained for this (Matryoshka models, such as Qwen3-Embedding or
  nomic-embed-text) lose little meaning; smaller vectors download and
  compare faster. More than the model gives is an error that the
  embed job reports (with the number to use).
- `query_prefix` - put in front of every query, not of the descriptions.
  The default suits mdbr-leaf-ir, snowflake-arctic-embed and bge models.
  Qwen3-Embedding wants an instruction instead, such as `"Instruct: Given
  a description of a photo someone is looking for, find photo descriptions
  that match it\nQuery: "`; set `""` for models that want none, or the
  prefix your model's card asks for (`"search_query: "` for
  nomic-embed-text).
- `batch` - descriptions sent in one request (1 to 256, default 32; a
  batch of a small model takes a fraction of a second on a CPU). A
  search typed while an embed job runs waits for the batch in progress:
  with a large model on a CPU, a smaller batch (8) keeps that wait short.
- `allow_cloud` - an endpoint that is not on this computer or the local
  network is only used when this is `true` (default `false`): the
  descriptions, and every typed query, then go to that service. Hidden
  pictures are never sent to one.
- `model_file` - the GGUF file the endpoint runs (a relative path is next
  to `config.json`); `""` (the default) means the model of the
  `local_server` entry with `"kind": "embeddings"` that serves the
  endpoint, if there is one. The vectors are tied to this file (another
  file makes every vector again), a llama.cpp server that says it runs a
  model file of another name is refused, and a published site can only
  ship the model with it.
- `dense_file` - the model's Dense head (sentence-transformers'
  `2_Dense/model.safetensors`, F32), when it has one: mdbr-leaf-ir ends in
  a layer that turns its 384 numbers into 768, which llama.cpp leaves out.
  With it, every request asks for the model's raw vectors (the request
  field `"embd_normalize": -1`) and the layer is applied here, to pictures
  and searches alike (only its first `dims` rows). Only a llama.cpp
  server gives raw vectors: one that answers with unit-length vectors
  anyway (an Ollama endpoint, or a llama.cpp build too old for the field)
  is refused with a message that says so, and nothing is stored.
- `license_file` - the model's license text (with a line saying where the
  model comes from). A published site ships the model only with it: it
  goes along as `data/meaning/LICENSE.txt`.
- `credit` - one line the pages of a published site show at their foot
  next to the license link, such as `"mdbr-leaf-ir by MongoDB
  (Apache-2.0)"`.

Every embed run that embeds anything also embeds two fixed, made-up
sentences and keeps their vectors (a "probe"): when they come out
differently later, another model answers the endpoint, and every picture
is embedded again. A published site checks itself with them too. (Vectors
made before there was a probe get one from the next **Update meaning
search**, or the next run that embeds something.) A text too long for the
model (rare: a picture with a lot of text in it) is cut and tried again,
the text read in the picture first; the job's summary counts the pictures
cut.

Changing `endpoint`'s model, `model`, `dims`, `model_file` or
`dense_file` makes every vector again (the next **Update meaning
search**, scan, describe or refine job does it; until then the search
goes by the words, and "More like this" uses the older vectors). When
the model file or the Dense head cannot be read (a renamed folder, a
drive that is not there), nothing is embedded and the vectors stay as
they are; the Manage page says why, and the search goes by the words
until the file is back. A picture whose title, description or tags you
edit is left out of search by meaning until it is embedded again. The
vectors live in `data/vectors.db`, which is safe to delete: it is made
again.

## image_embedding

Optional: looks ("Looks like this", and finding pictures by a picture
dropped on the search page; see "Looks like this and search by a picture"
in the README). An image embedding model turns what each picture looks
like into a look vector, kept in `data/image_vectors.db`.

```
"image_embedding": {
    "endpoint": "looks",
    "dims": 256,
    "max_edge": 1024,
    "batch": 8,
    "model_file": "",
    "mmproj_file": "",
    "probe_check": true,
    "opening_views": true,
    "text_prefix": "task: search result | query: "
}
```

The model must embed pictures through llama.cpp's server: for example
Google's EmbeddingGemma 2 (Apache-2.0: a model file and an image projector
file, both GGUF; llama.cpp release b11452 or later). Run it with the
projector and flash attention off:

    llama-server -m model.gguf --mmproj model.mmproj.gguf --embeddings -fa off --host 127.0.0.1 --port 8097 -c 8192 -ub 8192 -b 8192

or with a `local_server` entry of `"kind": "embeddings"` that names the
projector in `mmproj` (it passes `-fa off` itself). On a CPU a picture
takes a few seconds (hours for a large library); a graphics card makes
them much faster, so **Update looks** is a job of its own, never part of a
scan. A search by a dropped picture embeds that one picture, on whichever
server the endpoint reaches (a CPU server takes a few seconds; a stopped
server is started for it with its `auto_start`).

- `endpoint` - the name of an endpoint (in `endpoints`, an
  OpenAI-compatible one) that serves the image model; `""` (the default)
  turns looks off. It must be on this computer or the local network:
  pictures (hidden ones too) are never sent to a cloud service for this.
- `dims` - keep this many numbers of each vector (the first ones, scaled
  back to unit length): 256 by default (about 350 bytes a picture in a
  published site), 0 for all. EmbeddingGemma 2 gives 768 and keeps what
  pictures look like when cut to 256.
- `max_edge` - the long edge, in pixels, of the picture the model is sent
  (320 to 2048, default 1024; never larger than the picture). A larger
  picture costs the model much more time; 768 halves it.
- `batch` - pictures sent in one request (1 to 64, default 8).
- `model_file`, `mmproj_file` - the model and projector files the
  endpoint runs (relative paths are next to `config.json`); `""` (the
  default) means those of the `local_server` entry that serves the
  endpoint, if there is one. The looks are tied to them (see below), and a
  llama.cpp server that says it runs a model file of another name is
  refused.
- `probe_check` - `true` (the default): before the first batch of every
  run, three probe pictures drawn by the program (a gradient, a disc,
  stripes) are embedded and compared with the vectors the first run made
  of them; when they differ (cosine below 0.99) the run stops with a
  message, as a graphics card running the model with flash attention on,
  or in float16, makes different vectors. Make the first run (a few
  pictures: `scan.py --embed-images --limit 8`) on a CPU server, or with
  flash attention off, to have a trusted reference. The reference belongs
  to the model and projector files: a rebuild for other `dims` or
  `max_edge` keeps it. When the check fails, fix the server (flash
  attention off, not float16) and update the looks again; if the server
  was right and the reference was not (made on a server that was not), the
  Manage page offers **Rebuild looks**, which throws away the reference
  and the kept looks and makes them again. `false` skips the check.
- `opening_views` - `true` (the default): **Update looks** also works out
  where described 360 photos open (see "Where 360 pictures open" in the
  README): the model embeds views of each photo every 45 degrees round
  the horizon and the photo's title with the first sentence of its
  description, and the photo opens at the view nearest its words when that
  beats the front by enough. A model that does not match words to pictures
  (checked first with the probe pictures and words for them) leaves the
  photos at the front. `false`: no views are worked out, and the next run
  forgets those it made (the photos open at the front again; views chosen
  with **Open here** stay).
- `text_prefix` - what the photo's words are sent after (at most 200
  characters): EmbeddingGemma's query prompt by default; another model may
  want another (or `""`). The views are worked out again when it changes,
  as when the model, a photo's words or its stitch change.

The looks are tied to a recipe: the model and projector files, `dims`,
`max_edge` and the way pictures are prepared. When it changes, the kept
looks still work for **Looks like this** (they match each other), but
pictures dropped on the search page cannot be compared with them, and no
new ones are made until you choose **Rebuild looks** on the Manage page
(or run `scan.py --embed-images --rebuild-looks`): that throws them all
away and makes them again. A rebuild never throws away looks of the
current recipe, so one that was stopped (or is run again, for another
library) goes on where it was; to make looks of the same recipe again,
delete `data/image_vectors.db`. Copies of a picture share one look vector
(it belongs to the picture's content); vectors of pictures no longer in
the index are dropped by the next run, or at once when a library's
indexed data is deleted or the scanner removes pictures missing too long.
A `data/image_vectors.db` written by a newer version of the program is
never changed: there are no looks until that version is used again (or the
file is deleted).

The kept looks also stack duplicates on the search page and the Duplicates
page, with "Same picture, any date" there (see "Duplicates by look" in the
README). That needs no setting: it uses whatever looks are kept, even of an
older recipe, and the endpoint may be off. The cut-offs are the same for
every library and were set for EmbeddingGemma 2 at 256 dimensions; another
model may need others (they are constants in `search_index.py`:
`LOOK_STACK_COSINE`, `LOOK_STACK_SECONDS`, `LOOK_SAME_SECONDS`,
`LOOK_APART`, `LOOK_APART_LATER`, `SAME_PICTURE_COSINE`). Deleting
`data/image_vectors.db` puts the stacks back as the dHashes alone make
them.

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
The fills apply to 360-degree videos too: to their posters and contact
sheets (made again the same way), and to the viewer, which fills the
poles of each frame as it plays.

## level_horizon

360-degree videos of an Insta360 X4 or ONE R, and the unstitched 360
photos of a ONE R, are levelled with the camera's motion sensor: the
panorama is turned so that its horizon stays level however the camera
was held (only the tilt is taken out; the middle of the panorama still
looks where the camera looked). It applies to the posters, contact sheets
and stitched photos, and to the viewer, which turns each frame of a 360
video as it plays (where the viewer's own **Level** button can turn it
off for one browser). On by default; set `"level_horizon": false` to see
everything as the camera did. After a change, the next scan stitches the
360 photos and videos in the index again (descriptions are kept; their
looks are made again by the next Update looks), and the viewer and
published sites no longer get the level tracks (an export publishes a
video's track with the video, when this is on).

## Videos: ffmpeg, video_frames, video_sheet_check, video_proxies

Videos (`.mov`, `.mp4`, `.m4v`, `.3gp`, `.3g2`, `.avi`, `.mkv`, `.webm`,
`.wmv`, `.mts`, `.m2ts`, `.mpg`, `.mpeg`, and Insta360 360-degree videos,
`.insv`) are indexed when the programs
`ffmpeg` and `ffprobe` (which come together in every ffmpeg download) can be
found, and the library's `videos` setting is on (see README, "Videos").

- `ffmpeg` - the ffmpeg program, or the folder holding it, such as
  `C:\path\to\ffmpeg\bin\ffmpeg.exe` or `/usr/local/bin/ffmpeg` (a
  relative path is relative to the folder of config.json); `ffprobe` is
  looked for next to it, then on the PATH. `""` (the default) looks for
  both on the PATH. Without them, scans list videos but do not read them
  (videos indexed before stay as they are), and the Manage page says so
  once. Frames are always decoded on the processor (no graphics card).
- `video_frames` - how many frames a video's contact sheet holds for
  describing (1 to 9, default 4: a 2 x 2 grid of frames at 12.5 %, 37.5 %,
  62.5 % and 87.5 % of the video). The sheet is as large as the library's
  endpoint's `max_image_edge` when it is made (a later change of the
  endpoint keeps the sheets as they are; the describe step shrinks a
  larger one). After a change of `video_frames`, the next scan (or **Build
  previews**) makes the sheets of the videos already indexed again, which
  reads each video once more. A very short video's sheet may hold fewer
  frames; the model is told how many it holds, and that the title and
  description are about what happens in the video, never about the frames
  or the grid (prompt version 6: videos described before it are out of
  date, see README, "Upgrading").
- `video_sheet_check` - `true` (the default) checks every new description
  of a video or a drive for words about the contact sheet instead of the
  video ("four frames show ...", "the top-left panel", "in each frame",
  "a sequence of dashcam footage"; English only) and then asks the same
  endpoint once more, with one more sentence in the prompt: one extra
  answer at most (the request waits out rate limits like any other),
  counted in the tokens and the cost. The second answer is kept when it
  is free of such words, otherwise the one with fewer of them (the first
  on a tie, or when the second was cut off and the first was not); the
  provenance says `"reasked": "sheet"` (not when the second request got
  no answer: then it stays out of date). Current video and drive
  descriptions with such words that were not asked again count as out of
  date (**Re-describe out of date**, `scan.py --redescribe-stale`).
  `false` sends no second request and counts none of them as out of
  date.
- `video_proxies` - playable copies, off by default. Browsers play H.264
  videos, but many cannot play HEVC (most phones' format in recent years)
  or older formats (AVI, WMV, MPEG-4 Part 2). With `"enabled": true`, the
  Manage page offers **Make playable copies (N)** for a library: a job
  that writes an H.264 / AAC copy of each such video into
  `data/proxies/<library id>/`, at most `height` pixels on its short side
  (default 720) with quality `crf` (14 to 40, default 26: lower is better
  and larger). The viewer then plays the copy, and an export with videos
  can publish it. Copies of videos that changed or are gone are removed by
  the next run (a moved or renamed video keeps its copy; deleting a
  library's indexed data deletes its copies); the Manage page shows how
  much space they take. Encoding takes about as long as the video plays,
  or longer. A 360-degree video plays its LRV and needs no copy; one
  without an LRV (there when the job runs) gets a copy with both lenses
  side by side, `height` pixels tall and twice as wide (the viewer
  stitches it as it plays), whatever their codec: no browser plays two
  lens streams, or two files, as one video.

When a video was taken has no setting: it comes from the video's own
metadata and its file name (see README, "Videos"). A dashcam (or another
camera that knows no time zone) writes its clock's time as if it were
UTC; its videos take the time in their names, and the next scan reads
the dates of videos an older version dated hours off that way again
(ffprobe only, once per video).

## Drives: drives, drive_previews

A drive is a run of video clips a camera recorded one after the other (a
dashcam's one-minute files; see README, "Drives"). Both settings are
optional.

- `drives` - how drives are found: `{"slack": 20, "min_clips": 3}`.
  `slack` (0 to 600 seconds, default 20) is how long after one clip ended
  the next one may start; `min_clips` (2 or more, default 3) how many
  clips a drive has at least. Clips are taken from one folder, with names
  of the same pattern (every run of digits counts as one), of the same
  size and codec, and every clip of a drive but the last is as long as
  the other clips of its recording (at least 30 s): a shorter one ends
  the drive. A change shows on the search page at once; descriptions of
  drives that changed are made again by the next describe run.
- `drive_previews` - sped-up previews, off by default:
  `{"enabled": false, "speed": 60, "height": 720}`. With `"enabled": true`,
  the Manage page offers **Make sped-up drives (N)** for a library with
  drives: a job that writes an H.264 copy of each drive, `speed` times
  faster (2 to 600, default 60), at most `height` pixels on its short side
  (144 to 1080, default 720), 30 frames a second and without sound, into
  `data/drive-previews/<library id>/`. The viewer plays it on request (or
  by itself where the clips cannot be played). Previews of drives that
  changed or are gone, or of other settings, are removed by the next run;
  deleting a library's indexed data deletes them too.

## gps_check

The GPS check sets aside GPS positions that cannot be where a picture was
taken (see README, "Positions that cannot be right"). It runs after every
scan, **Build previews** and **Add place names** (also one that was
stopped) and before an export with locations, over the pictures of every
library, and decides again each time; a change of these settings takes
effect with the next of those (**Use this location anyway** and **Ignore
location** in the viewer at once). All keys are optional:

```
"gps_check": {
    "enabled": true,
    "max_speed": 1300,
    "window_hours": 12,
    "neighbours": 4,
    "stale_minutes": 5,
    "repeat_hours": 1,
    "across_libraries": true
}
```

- `enabled` - `false` turns the check off: every position it set aside
  comes back with the next run. Your own choices in the viewer still
  apply.
- `max_speed` - km/h (300 to 5000, default 1300: an airliner with the jet
  stream behind it): a picture is too far from another when this speed is
  not enough to get from one to the other in the time between them (less
  1 km and the accuracy each position states, plus 2 minutes for the
  clocks). Lower values start to set aside real positions taken from an
  aeroplane.
- `window_hours` - how far in time (1 to 72, default 12) pictures count
  as neighbours.
- `neighbours` - how many of the pictures taken nearest in time vote (2 to
  9, default 4): a position is set aside when more than half of them, and
  at least 2, are too far away. Copies of one shot count once.
- `stale_minutes` - a GPS fix that is older than the picture by more than
  this many minutes (1 to 240, default 5) is set aside when the speed it
  states would have taken the camera more than 2 km away in that time, or
  when it is more than 15 minutes old (a fix of a phone standing still
  stays right for a while). Only cameras that save the time of the fix
  (most phones) can tell, and only those whose other pictures mostly have
  fresh fixes. Without the picture's time zone every offset it may have
  had must make the fix that old.
- `repeat_hours` - a camera without a GPS receiver of its own that saves
  no GPS time and no accuracy and gave exactly the same position to
  pictures taken more than this many hours apart (0.25 to 72, default 1),
  with no other position in between and no week without a picture, is
  reporting its last known position: such a position is never a
  reference for others, and it is set aside when more than half of the
  pictures compared contradict it, even if that is only one.
- `across_libraries` - pictures of all libraries are neighbours of each
  other (default `true`: a phone in one library vouches for a camera in
  another). Set `false` when your libraries hold the pictures of people
  who travel apart. The place estimates follow it too.

Older versions had `estimate_places` and `estimate_minutes` here; they
now stand in for `place_estimates`' `enabled` and `window_minutes` when
those are not given.

## place_estimates

Places estimated for pictures without a GPS position of their own (no
position in the file, or one the GPS check set aside), from a copy of a
located picture, the pictures taken around the same time or the
library's area (see README, "Estimated places"). They are worked out
again after every GPS check, so a change here takes effect with the next
scan (or **Build previews**, **Add place names**, an export with
locations, or a choice in the viewer). All keys are optional:

```
"place_estimates": {
    "enabled": true,
    "window_minutes": 30,
    "max_spread_km": 2,
    "library_area": true
}
```

- `enabled` - `false` turns the estimates off (the places you set or
  accepted in the viewer stay, and so do the places the pictures' files
  name).
- `window_minutes` - how far in time (1 to 240, default 30) a located
  picture may be from the picture to count; on one side only, the nearest
  must be within half of it. (Also how far apart a picture without a
  position and the one whose time zone it takes for its local time may be,
  see `local_times`.)
- `max_spread_km` - how far apart (0.1 to 50, default 2) the located
  pictures before and after it may be, how far off their own positions
  may be by their own account, and how far the camera may have moved
  since the nearest one on one side (by the speed of the shots there, at
  least walking pace, and after two minutes at least 50 km/h) or between
  a copy and the picture that looks like it: further apart, and there is
  no estimate (a drive, a flight); further off, and the picture gets the
  nearest shot's town without a position (the region beyond 10 km,
  nothing beyond 25 km). Larger values place more pictures, and more of
  them wrongly.
- `library_area` - a picture that gets no place otherwise gets its
  library's region (no position, never on the map) when at least 95% of
  the library's located pictures (and at least 20) are within 100 km of
  each other and name one region (or country), and the picture was taken
  within 3 hours of them with no located picture somewhere else taken
  nearer in time -- or it has no date to go by and the located pictures
  are at least a tenth of the library's (default `true`). Set `false`
  when a library mixes trips.

## gps_tracks

The GPS tracks dashcams (and other cameras) record into their videos:
one fix a second, as Novatek-style `freeGPS` blocks (an NMEA sentence, or
binary fields in a layout exiftool documents) or NMEA sentences in a text
track (see README, "Routes"). A scan reads them with each new or changed
video, and the videos indexed before; a clip with a track gets the
position of its middle fix and, when its file names no time zone, the UTC
offset its clock has by the GPS time (lent only to the same camera's
pictures); drives get routes on the map and in the viewer. Optional:

```
"gps_tracks": {
    "enabled": true
}
```

- `enabled` - `false` stops reading tracks, and the next scan takes away
  the positions they gave (the tracks read are kept: with `true` again
  the positions come back without reading the videos again). Routes are
  no longer shown, nor exported.

`"gps_tracks": false` means the same as `{"enabled": false}`.

## plain_language

Plain-language search (README, "Plain language"): the search box
understands the names of people and places, kinds of place and dates in
what is typed (`anna at the zoo last summer`) and shows how as chips over
the results.
On by default:

```
"plain_language": true
```

- `false` (or `{"enabled": false}`) turns it off: every page (search, map,
  albums) searches what is typed as it is typed, with no chips and no
  questions, and the search tips leave it out. A site published while it
  is off is off too. Words that are names are still searched by their
  words only when searching by words and meaning (README, "Search by
  meaning"): that is not part of this switch.

## trips

Trips: libraries whose folder is named like a trip and runs of days away
from home (see README, "Trips"). They are worked out whenever the Trips
page is opened, so a change here shows at once. All keys are optional:

```
"trips": {
    "enabled": true,
    "library_folders": true,
    "away_km": 50,
    "max_gap_days": 1,
    "min_pictures": 10,
    "best_per_day": 6,
    "home": ""
}
```

- `enabled` - `false` turns the trips off (the Trips page says so).
- `library_folders` - a library whose folder name starts with a date
  ("2024-05-12 - Lakes") is a trip (default `true`).
- `away_km` - how far from home (5 to 20000 km, default 50) a picture's
  GPS position makes its day a day away.
- `max_gap_days` - days away with at most this many days between them
  (0 to 7, default 1) are one trip, unless a picture of a day between was
  taken at home. Days away up to 3 days apart in one area (the last GPS
  position before the gap and the first after it at most 500 km apart)
  join too, also with `0`: days without pictures in the middle of a stay.
- `min_pictures` - the fewest pictures (1 to 10000, default 10) a run of
  days away needs to be a trip, or half as many taken on two days or
  more. A library named like a trip is one however few it holds.
  Visitors see a run of days away only when the pictures they may see
  (not hidden) make one.
- `best_per_day` - how many of a day's best pictures its strip shows (1 to
  24, default 6).
- `home` - where home is, as `"lat, lon"` (or `[lat, lon]`), when the
  automatic choice (where pictures were taken on the most days) is wrong,
  for instance after a move. It is used to tell the days away and to keep
  the area around it out of a trip's export; it is never shown or
  published.

A trip's export (README, "Publishing a trip") takes the options of a
library's `export` below (but `album`), and one more:

- `home_km` - the size of the squares of the area around home that a
  trip's export never shows: 5, 10 (default), 20, 40, 80 or 160 km, or 0
  (home is not kept out): no position, town, estimated place or drive
  route in it, no RAW file holding a position in it (or of a picture taken
  there), and no dashcam clip filmed there (or without a GPS fix),
  nor its drive's words, unless chosen in the review. The sizes' grids
  nest, so exports made with different sizes tell no more together than
  the smallest; a size kept from before that is none of these is read as
  the default.
- `story` - `true` publishes the trip's story (see "trip_stories" below),
  unless it is hidden, and only with `location` (the story names the
  trip's places) (default `false`).

They are kept with the trip in `data/trips.json` (`"exports"`), not in
config.json: the Export dialog writes them when a trip's export starts.

`"trips": false` means the same as `{"enabled": false}`.

## trip_stories

The trips' stories (see README, "Trip stories"): a paragraph a day written
by a language model from each trip's facts (text only). All keys are
optional:

```
"trip_stories": {
    "endpoint": "",
    "pictures": 6,
    "prompt_tokens": 6000,
    "notes": ""
}
```

- `endpoint` - the endpoint (or group) that writes them. Empty (the
  default): the `refine` endpoint of the trip's libraries (its own first),
  else their describe endpoint. An endpoint of a `local_server` is started
  for the job and stopped afterwards, and the job stops before its
  `stop_at` time. Only an endpoint on this computer or your local network
  gets the names of the people (People); a cloud endpoint gets the facts
  without them, and its stories name nobody.
- `pictures` - how many of a day's best pictures the facts describe (1 to
  24, default 6). More gives the model more to tell, and longer prompts.
- `prompt_tokens` - about how many tokens one request's prompt may hold
  (1000 to 200000, default 6000); a longer trip is written in several
  requests of a few days each. For an endpoint of a `local_server`, also
  no more than its `ctx_per_slot` allows besides the answer (the answer
  gets up to 4000 tokens, a third of `ctx_per_slot` at most).
- `notes` - your words added to the prompt (up to 1000 characters), for
  instance who the people are, or the language or voice to write in: "We
  are a family of four. Write in German." The model is still told to use
  only the facts.

## people

Optional, off by default: People, the faces in the pictures (see
"People" in the README). A face detector finds the faces of every
picture and a face recogniser makes a vector of each, on this computer's
processor, in a helper program that runs with a Python environment of its
own; then the faces are grouped into people, which you name on the
People page. The faces, the people and their names are kept in
`data/faces.db`, never sent anywhere (but for the names, to the model that
writes trip stories when it runs on this computer or your local network:
see "trip_stories") and never exported (but for the names, with an
export's `people_names` option). Every key is optional
except `enabled`, `python` and `models_dir` (or full paths in `detector`
and `recognizer`); all of them are set in `config.json` only (no page
changes them).

```
"people": {
    "enabled": true,
    "python": "C:\path\to\people-env",
    "models_dir": "C:\path\to\models\faces",
    "detector": "face_detection_yunet_2023mar.onnx",
    "recognizer": "face_recognition_sface_2021dec.onnx",
    "after_scan": true,
    "workers": 4,
    "threads": 1,
    "max_edge": 2048,
    "min_score": 0.7,
    "min_size": 20,
    "max_faces": 64,
    "full_size": true,
    "full_megapixels": 60,
    "frames": 12,
    "frame_seconds": 4,
    "drives": false,
    "match": 0.4,
    "group_quality": 0.25,
    "min_faces": 3,
    "visitors": false
}
```

- `enabled` - `true` turns People on. With `false` (the default) no face
  is looked for, others (`visitors`) see nothing of People, and the
  Manage page says nothing about faces unless face data from before is
  kept (it then offers to delete it; you still see what is kept).
- `python` - the python of the people environment: a Python 3.9 or later
  with numpy and OpenCV (`opencv-python-headless`; see
  `requirements-people.txt` and the README), or the folder of that
  virtual environment (its `Scripts\python.exe` or `bin/python` is used).
  A relative path is relative to the folder of `config.json`. The helper
  (`people_worker.py`, in the program folder) runs with it, isolated from
  the environment variables and folders of the search programs (`-I`),
  and gets the pictures through a pipe. It gets only the environment
  variables a Python needs (no API keys) and those of OpenCV, OpenMP and
  OpenBLAS (OpenBLAS and OpenMP set to one thread unless you set them).
  Do not install those packages into the Python that runs the search
  programs: they do not need them. `requirements-people-pinned.txt` has
  the versions People was tested with, and their hashes.
- `models_dir` - the folder with the two model files (relative to the
  folder of `config.json`).
- `detector`, `recognizer` - the model files' names in `models_dir` (or
  their own paths): by default YuNet (`face_detection_yunet_2023mar.onnx`,
  MIT) and SFace (`face_recognition_sface_2021dec.onnx`, Apache-2.0) from
  the OpenCV Zoo. The faces found are tied to the files' contents: other
  files (or the settings below that change what is found) make the next
  run look at every picture again; a face found again where it was keeps
  what is known about it.
- `after_scan` - `true` (the default): every scan ends by looking for the
  faces of the pictures it found (only when People is on and set up; a
  setup that does not work is a warning in the scan's log, not a failed
  scan). `false` leaves that to **Find faces** on the Manage page and
  `python people.py`.
- `workers` - how many helpers run side by side (1 to 16, default 4), each
  looking at one picture at a time with `threads` threads of OpenCV (0 to
  64, default 1; 0 lets OpenCV choose, which is all the processor's
  cores). More helpers find faces faster and keep more of the processor
  busy; each one holds OpenCV and the models in memory (about 300 MB,
  up to about 450 MB while it looks at a large picture, and up to about
  1 GB while it looks at one in full size, see `full_size`: at most two
  helpers do so at a time). Videos are read with ffmpeg in this program
  (see `frames`), at most four at a time.
- `max_edge` - the largest size a picture is looked at (its long edge in
  pixels, 320 to 4096, default 2048; pictures are looked at in their
  stored preview, at most 2048 pixels, so larger values only matter for
  libraries without previews). Each picture is looked at at that size and
  at half of it, a quarter, ... down to 256 pixels, as the detector only
  finds faces of about 10 to 300 pixels: small faces at the large sizes,
  large ones at the small sizes. 1024 takes about a third of the time and
  misses some small faces.
- `min_score` - how sure the detector must be that it found a face (0.3
  to 0.99, default 0.7). Lower values find more faces in profile and in
  the background, and more things that are not faces (toys, statues,
  wheels); each face keeps its score and a quality (0 to 1, from its
  score, size, sharpness and how much it faces the camera), so what uses
  the faces can choose the good ones.
- `min_size` - the smallest face kept (its box's short side, 8 to 400
  pixels of the picture looked at, default 20).
- `max_faces` - at most this many faces per picture, the surest first (1
  to 500, default 64).
- `full_size` - `true` (the default): wide panoramas (at least 2.2 times
  as wide as they are high, or as high as they are wide: the pictures the
  viewer pans along) and very large pictures (at least `full_megapixels`
  million pixels, default 60; 5 to 1000) are looked at in full size, read
  from their original files (at most 100 million pixels), when they are
  larger than `max_edge` on their long edge: in a 2048 pixel preview of a
  panorama the people are a few pixels high, too small to be found.
  The helper looks at such a picture in tiles of at most 2048 pixels
  (which overlap, so a face on the boundary of two is seen whole), at
  every size down to 256 pixels; that takes many times as long as a
  picture looked at in its preview (it holds many times the pixels), and
  a small picture of each face found is kept in `data/faces.db`, as the
  preview is too small to show them on the People page. Their library's
  folder must be reachable (they wait for a later run when it is not).
  Changing `full_size` or `full_megapixels` only looks again at the
  pictures it concerns; the faces found again where they were keep what
  you said about them (names, moves), and a face you put in a person stays
  even when the full size does not show it again. An original that takes
  longer than 5 minutes to read waits for a later run when its folder went
  away meanwhile (else it counts as a failed try). 360-degree photos are looked at in
  their previews, as before (videos: see `frames`). `false` looks at
  every picture in its preview.
- `frames` - the most frames of a video looked at (0 to 60, default 12):
  its poster frame and about one every `frame_seconds` seconds (0.5 to
  600, default 4), spread evenly over it (a 20 second clip: 5 frames; a
  minute or longer: 12). The frames are read from the video's file with
  ffmpeg on the processor (never with hardware decoding; the `ffmpeg`
  setting, see Videos), at most `max_edge` pixels, so its library's folder
  must be reachable (videos wait for a later run when it is not, or when
  ffmpeg is missing). A 360-degree video's frames are stitched panoramas,
  as its poster is, of which only the faces near the horizon are kept.
  The faces of one person in a video's frames are linked by how alike
  they are (never two faces of one frame) and kept as one face: the best
  of them, with a small picture of it, the second it was seen at (where
  the video's page starts it when you open it from the People page),
  and every moment that person was seen at (the viewer's moments). A face
  stays with that best one only when it is at least 0.4 alike to it (the
  default `match`), or a little less where a face that stays was in
  another frame (one person turning away): two people who look a little
  alike stay two. A face seen in a single frame and too blurred to tell
  who it is (a quality below 0.1) is left out, and so are the faces past
  the 32 best of a video (a crowd). The faces found before in a video's
  poster keep what you said about them when they are found again as the
  same person; a face you put in a person stays even when the frames do
  not show it again. Each frame takes about as long as a picture looked
  at in its preview, and ffmpeg's time to read it (longer for 4K videos).
  A video whose frames take longer than 10 minutes to read, or whose
  folder goes away while they are read, waits for a later run (or counts
  as a failed try when its folder is still there); Stop is heard between
  frames. Changing `frames` or `frame_seconds` only looks again at the
  videos. `0` looks at a video's poster (its preview) alone, as before.
- `drives` - `false` (the default): the clips of a dashcam's drives
  (see Drives: a drive whose camera cut its clips at most 5 minutes long)
  are not looked at for faces -- they mostly show other drivers -- and
  the faces found in them before go, but for those in a person (yours or
  the grouping's); the Manage page counts them as skipped. The chapters
  of a longer recording (action cameras cut one into files by their size,
  mostly longer than 5 minutes) are looked at as any video, and so are a
  dashcam's clips that make no drive: a recording of fewer clips than the
  drives' `min_clips`, or a clip copied into another folder. With
  `frames` 0 (posters alone) no clip is skipped, as before. `true` looks
  at every drive's clips as at any video.
- `match` - how alike faces must be to be one person (0.2 to 0.9,
  default 0.4): the faces are grouped by average linkage, two groups
  becoming one while the cosine of their faces' vectors, on average over
  every pair, is at least this; a face of lower quality joins the person
  whose faces it is most like when it is that alike (0.1 more alike for
  a person of fewer than 6 faces in its group: an average over a few
  faces is unsure), and that person has no other face in its picture.
  Two faces of one picture are never grouped together. Higher values mix
  up fewer people (relatives, babies) and split one person (sunglasses,
  profiles, different ages) into more groups. The recogniser's own
  threshold for "the same person" is about 0.36.
- `group_quality` - the faces of at least this quality (0 to 1, default
  0.25) are grouped; the others (small, blurred, turned away) only join a
  person afterwards. The viewer's suggestions of who a face is (and the
  People page's **Who's this?**) measure it against these faces of each
  named person, whoever put them there (all
  of its faces while it has fewer than five such; it is then never
  marked) (README, "Naming a face in the viewer").
- `min_faces` - a group of at least this many faces (1 to 100, default 3)
  is a person, when they are in pictures of at least two different
  moments: pictures whose dHashes are at most 10 bits apart (copies of one
  shot) or that were taken within 2 seconds of each other (a burst) are
  one moment, so a burst or copies of one shot are nobody on their own.
  Smaller groups stay among the other faces (a person you named stays a
  person whatever its size).
- `visitors` - `true` (with `enabled` true) shows people to others who
  use the search pages from another computer (`serve.py --host`): the
  People page (named people that are not hidden, and their faces), the
  names in the viewer, `person:` and the smart albums that search for a
  person; never the boxes of the faces (nor naming a face in the viewer:
  that is yours only), unnamed or hidden people, or anything of hidden
  pictures. Default `false`: people are this
  computer's only (smart albums with `person:` are not listed for
  others).

Changing `match`, `group_quality` or `min_faces` takes effect with the next
grouping: **Group faces again** on the People page (or the next run that
finds faces); the viewer's suggestions follow `group_quality` at once.
What you said about people (names, merges, moved faces, "not this
person", hidden people, "not a face") is kept.

`python people.py --check` starts the helper, loads the models and looks
for a face drawn by the program; it says what is wrong when that fails.

## local_times

Pictures whose camera's clock was not the local time of where they were
taken -- a dashcam that ignores daylight saving, a phone on the time of
the airport it left, a camera on home time abroad -- get the local time
of their place, with that place's UTC offset (see README, "Times in the
local time of the place"), when the moment they were taken is known for
sure: by a video's GPS track, by the GPS time or GPS date saved with the
picture, or by a picture of the same camera taken within 3 hours that has
one of those (and whose clock can be this one's; a picture without a
position takes its time zone only within `place_estimates`'
`window_minutes`). The time zone of a place is the nearest town's in the
place names (`data/places/places.db`, built again from the downloaded
files when it has none), its offset at that moment comes from Python's
time zone database (the `tzdata` package where the system has none, as on
Windows). Without the place names or the time zone database nothing
changes. The camera's own clock is kept beside the local time and shown
in the picture's details. Worked out again after every scan, **Build
previews** and **Add place names**, before an export with the location,
and when you use or ignore a picture's position or set its place in the
viewer. Optional:

```
"local_times": {
    "enabled": true
}
```

- `enabled` - `false` gives every picture its camera's time back at the
  next scan (or **Build previews**, or **Add place names**), and keeps it
  so; `true` again works the local times out again.

`"local_times": false` means the same as `{"enabled": false}`.
`python local_times.py --list` shows what a scan would give each picture,
changing nothing.

## raw

Camera RAW files (`.dng`, `.arw`, `.cr2`, `.cr3`, `.nef`, `.nrw`, `.orf`,
`.rw2`, `.raf`, `.pef`, `.srw`; see README, "RAW photos"). A RAW file with
a picture of the same shot beside it (the same name in its folder, else
the one picture of the same camera taken in the same second) is that
picture's RAW original: shown in its details, downloadable on this
computer, and the source of the EXIF facts the picture lacks. One without
is indexed by the largest preview the camera stored in it. Optional:

```
"raw": {
    "enabled": true,
    "index_alone": true
}
```

- `enabled` - `false` leaves RAW files out: they are not listed, the next
  scan takes them away from their pictures (which keep only their own
  EXIF) and marks the RAW files indexed on their own missing (they come
  back, with their descriptions, when it is `true` again within
  `purge_after_days`).
- `index_alone` - `false` still pairs RAW files with their pictures, but
  no longer indexes a RAW file without one (those indexed before are
  marked missing).

`"raw": false` means the same as `{"enabled": false}`. An export publishes
RAW files only with its `raw_files` option (see `export` under
`libraries`).

## street_maps

The street maps (README, "Street maps"): what `street_maps.py --download`
and **Download** on the Manage page download. All keys are optional:

```
"street_maps": {
    "radius_km": 20,
    "route_km": 5,
    "max_zoom": 15,
    "whole_degrees": true,
    "source": ""
}
```

- `radius_km` - kilometres around each place of a picture (its GPS
  position or estimated place; not of hidden pictures; 1 to 200, default
  20): a square that wide either way. Street maps of 5 GB or more are
  only downloaded when allowed a second time (`--allow-large`).
- `route_km` - kilometres either side of the drive routes (0 to 50,
  default 5; 0: none along them).
- `max_zoom` - the most detailed zoom downloaded (8 to 15, default 15:
  every building; 14, the streets, is two to three times smaller). The
  maps zoom three steps past it. The named places of pictures taken
  indoors use the building outlines, which only zoom 15 has (README,
  "Named places").
- `whole_degrees` - `true` (the default) asks the map's host for every
  tile of the whole-degree squares (about 111 km from south to north) the
  areas touch, and keeps only the areas: the host learns no more than
  those squares, and the download is larger than what is kept. `false`
  asks for the tiles kept only: a smaller download, but the host can see
  which areas they are.
- `source` - the address of a PMTiles file of the Protomaps basemap
  (version 4) to download from, for instance a build you keep on your own
  server; `""` (the default) is the newest build listed at
  build-metadata.protomaps.dev.

A size check or download made with other settings is not changed by
them: check the size again.

The map page's **Search an area**, **Street or place** reads the streets,
parks and places from the same tiles (README, "Searching an area of the
map"); there are no settings of its own. Street maps of `"max_zoom": 14`
name fewer small places (cafes, minor sights) there.

## named_places

The named places of the pictures on the street maps (README, "Named
places"): the park, beach, museum or cafe where each picture with a
position was taken, read from the downloaded street maps. All keys are
optional:

```
"named_places": {
    "enabled": true,
    "max_estimate_km": 0.3
}
```

- `enabled` - `false` turns them off: the pages, searches and exports
  leave the pictures' named places out at once (and with them what
  `kind:` and the kinds of place of plain language find: README, "Kinds
  of place"), none are looked up, and the next scan (or `python
  named_places.py --update`) deletes what was kept of them.
- `max_estimate_km` - an estimated place (see `place_estimates`) gets a
  named place only when it may be off by at most this much (0 to 2,
  default 0.3 km); estimates of more than 0.15 km get an area only (a
  park, a zoo) they lie deep enough inside of -- or the place the other
  pictures of the visit agree on, when it fits (README, "Named places").

The rules for pictures taken indoors (the street maps' building outlines,
what the picture shows, the pictures of a visit agreeing) have no
settings of their own: they need the street maps of `"max_zoom": 15` for
the building outlines (see `street_maps`) and described pictures.

Changing them takes effect at the next scan, or at once with **Find named
places** (or **Look up all again**) under Street maps on the Manage page.

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
  view, and for wide photos (panoramas at least 2.2 times as wide as they
  are tall, taller than their preview) a picture 1600 px tall for the
  viewer's pan view on phones. They take about 0.5 MB per picture (about
  50 GB per 100,000). With `false` they are made from the original each
  time they are shown.
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
- `videos` - `true` (the default) indexes the library's videos when ffmpeg
  is there (see "Videos" above); `false` leaves them out (videos indexed
  before are then treated like files excluded by an ignore pattern: marked
  missing, and removed after `purge_after_days`).
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
  differ from the defaults are stored). `export.py` uses them too, and has
  a switch for each that changes it for one run (`--file-names` for
  `file_names`, `--no-file-names` to turn it off; see `python export.py
  --help` and "Automating" in the README). Every option may be left out;
  the defaults publish as little as possible. A saved option an export
  cannot use (an unknown name, a wrong value, a title or footer line that
  is too long) is left at its default, never silently: the log of an
  export that runs with it (from the command line, say) has a warning for
  each, and the Export dialog shows them until the options are saved
  again (a title or footer line that is too long in its field, as saved,
  to be shortened):
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
    picture shows. Places you set or accepted in the viewer go with it
    like GPS positions.
  - `estimated_places` - with `location`, `true` also publishes the places
    estimated for pictures without a GPS position of their own (marked as
    estimated; hollow pins on the map); default `false` (see "Estimated
    places" in the README).
  - `routes` - with `location`, `true` also publishes the routes of drives
    and the positions video clips have from their GPS tracks (see
    `gps_tracks`); default `false`. Without it such clips keep their place
    names but not their positions (and so do places estimated from a
    track, also when you accepted them). The positions a dashcam's clips
    write out in their texts (as its overlay prints them) are never
    published.
  - `routes_trim` - metres of every route left out around where its drive
    starts and where it ends (0 to 5000, default 500; the drives of every
    library), with the positions of the clips and estimated places there:
    a drive often starts and ends at home. A line that passes that near is
    cut there too.
  - `street_maps` - with `location`, `true` also publishes the street maps
    (see `street_maps` above) around the published positions and routes,
    from zoom 9 on; default `false`. In a trip's export, no tile that
    touches the area around home goes.
  - `street_km` - with `street_maps`: kilometres around each published
    position (1 to 50, default 5; 2 km either side of the routes).
  - `street_zoom` - with `street_maps`: the most detailed zoom that goes
    (10 to 15, default 14).
  - `world_zoom` - with `location`: the most detailed zoom of the world
    map's tiles that go (2 to 12, default 5; zoom 6 alone has three times
    the tiles of zoom 0 to 5 together).
  - `file_names`, `folders` - `true` publishes file names or folder names
    (searchable); default `false`.
  - `exclude_types` - picture types left out; default
    `["screenshot", "document"]` (the others are `scan`, `artwork`, `meme`,
    `diagram`, `photo`, `other` and `video`: every video). A picture whose
    document type is a receipt, letter, form, ticket or handwriting also
    counts as a `document`, and one showing a chat, web page or app as a
    `screenshot`, whatever its photo type; a video counts as the kind the
    AI model saw in its frames too (a screen recording as a `screenshot`).
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
  - `credits` - a line of plain text shown at the foot of the pages (at
    most 4,000 characters: enough to name every creator of a mixed set of
    pictures). The map page, which has no foot, links to it instead
    ("Photo credits" in the corner of the map).
  - `zip` - `true` also writes `<folder>.zip` next to the output folder.
    An existing file of that name is only replaced when an earlier export
    made it and it was not changed since; otherwise the export stops.
  - `allow_indexing` - `true` lets search engines index the pages
    (default `false`: the pages and a `robots.txt` ask them not to list
    the pages or their pictures; anyone with the address can still open
    them).
  - `videos` - `"skip"` (the default) publishes each video as its poster
    frame; `"include"` also publishes the videos browsers play (H.264, or
    a playable copy made by `video_proxies`), copied into an MP4 without
    their metadata (no place, date, device, data tracks or chapters; the
    picture and sound are not re-encoded). Other videos (HEVC without a
    playable copy) keep their poster only; the review step counts them.
    The AI model checked only a few still frames of each video for
    private or sensitive content and never heard its sound: watch the
    videos you publish.
  - `max_video_mb` - videos larger than this many megabytes (default 200)
    are published as their poster only.
  - `raw_files` - `true` also publishes the camera RAW file of each
    published picture that has one (see `raw`), as the camera wrote it,
    in `raws/` (default `false`: RAW files never go out). Nothing can be
    taken out of a RAW file, so it carries every fact the camera wrote in
    it (the time to the second, the camera's serial number); one that
    holds a GPS position goes only with `location`, and only when its
    picture's own position goes (not one set aside; in a trip's export,
    neither that position nor the RAW file's own in the area around home,
    `home_km`).
  - `looks` - `true` (the default) publishes the look vectors of the
    published pictures (about 350 bytes each, in `data/looks-NNN.js`), so
    "Looks like this" works in the site too; nothing goes when no
    published picture has one (see `image_embedding`).
  - `similar` - `true` (the default) publishes the meaning vectors of the
    published pictures (about 350 bytes each, in `data/vectors-NNN.js`),
    so "More like this" goes by meaning in the site too. Only vectors made
    from a picture's current description go: a vector holds nothing its
    description does not show (no names, dates or places), and the
    model's name is not published. Pictures without one (search by
    meaning not set up, or not updated since the description changed)
    fall back to tags and dates there.
  - `meaning` - `true` (the default) also publishes the query model of
    search by meaning, when it can run in a browser, so that typed searches
    in the site find pictures by meaning as on the server: the model's files
    (`data/meaning/`, converted from `model_file` with its Dense head: about
    31 MB, of which visitors download about 23 MB with gzip, once, at their
    first search by meaning), the engine that runs it
    (`assets/embed-engine.js`, `embed-kernel.js`), the model's license and
    the credit line at the foot of the search and details pages. It works
    from disk and offline too. A web host that sends a
    `Content-Security-Policy` header must allow `'wasm-unsafe-eval'` in
    `script-src` for its WebAssembly part (else it runs in plain JavaScript:
    the same results, about five times slower). It needs `similar`, a
    `model_file` that can run in a browser (a small BERT-family model:
    mdbr-leaf-ir, snowflake-arctic-embed-xs; not Qwen3-Embedding or
    EmbeddingGemma), a `license_file`, and vectors made with that model
    (**Update meaning search**); the Export dialog says which is missing.
    The model files hold only public model data, the two probe sentences and
    their vectors, and the query prefix. Visitors who choose **Words** never
    download the model; with their browser's data saver on, a button asks
    first.
  - `album` - an album id (default `null`: the whole library): only the
    pictures of that manual album (see "Favorites, ratings and albums" in
    the README) are published, those of this library; the rules above
    still leave out what they leave out. The Export dialog lists the
    albums by name (`export.py --album NAME` takes a name, or the id as
    `#ID`; `--no-album` exports the whole library for one run). A
    smart album cannot be used: **Copy to a new album** on the Albums page
    makes a manual album of its pictures. An export whose album was
    deleted stops and says so (an album's id is never given to another
    album).
  - `marks` - `true` also publishes which pictures are your favorites,
    their ratings and your albums (the names of the albums that hold
    published pictures, in their order, and the smart albums with their
    searches as you saved them, without a library choice), so
    `is:favorite`, `rating:` and `album:` work in the site; default
    `false`: none of it is published, not even the name of the album the
    export is limited to. A smart album whose search cannot be published
    is left out: one limited to another library, one with `folder:`,
    `file:`, `place:` or `has:gps` (or the Place filter, or an area of
    the map) while `folders`, `file_names` or `location` is off, one with
    `kind:` while no named place is published (`location` off, or none of
    the pictures at one), one with `lib:`, and one that looks for
    pictures like one that is not published; a place or a kind of place
    that plain language understood is published as the words typed for it
    (a person too with `people_names`). A trip's export also leaves out a
    smart album whose area of the map comes into the area around home
    (`home_km`).
  - `people_names` - `true` also publishes the names of the people in the
    pictures (People: named people who are not hidden), as tags of the
    pictures they are in; default `false`. Faces, their boxes and
    anything else of People are never published.

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

With a model server the Settings page starts and stops, a stronger
model for the documents of the Family library, and search by meaning with
a small embedding model on the CPU (with its Dense head, license and
credit, so published sites can search by meaning too) that starts when
needed and stops after half an hour without searches (`local_server`,
`refine` and `embedding`, all optional):

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
        },
        {
            "name": "embed",
            "preset": "llamacpp",
            "base_url": "http://127.0.0.1:8099/v1"
        }
    ],
    "default_endpoint": "local",
    "embedding": {
        "endpoint": "embed",
        "dense_file": "C:\\path\\to\\models\\model.dense.safetensors",
        "license_file": "C:\\path\\to\\models\\model.LICENSE.txt",
        "credit": "mdbr-leaf-ir by MongoDB (Apache-2.0)"
    },
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
        },
        {
            "enabled": true,
            "kind": "embeddings",
            "endpoint": "embed",
            "binary": "C:\\path\\to\\llama-server.exe",
            "models_dir": "C:\\path\\to\\models",
            "model": "model.gguf",
            "port": 8099,
            "extra_args": ["--device", "none", "-t", "4"],
            "idle_stop_minutes": 30
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
