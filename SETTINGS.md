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
    "embedding": { ... },
    "image_embedding": { ... },
    "nadir_fill_deg": 0,
    "zenith_fill_deg": 0,
    "ffmpeg": "",
    "video_frames": 4,
    "video_proxies": { "enabled": false, "height": 720, "crf": 26 },
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
    "probe_check": true
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

## Videos: ffmpeg, video_frames, video_proxies

Videos (`.mov`, `.mp4`, `.m4v`, `.3gp`, `.3g2`, `.avi`, `.mkv`, `.webm`,
`.wmv`, `.mts`, `.m2ts`, `.mpg`, `.mpeg`) are indexed when the programs
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
  frames; the model is told how many it holds.
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
  or longer.

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
  - `credits` - a line of plain text shown at the foot of the pages.
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
