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
Videos (MOV, MP4, M4V, 3GP, AVI, MKV, WebM, WMV, MTS, MPEG, and Insta360
`.insv` 360-degree videos) are indexed too when ffmpeg is installed: see
[Videos](#videos).

360-degree photos are recognised: stitched panoramas (2:1 images tagged as
equirectangular, or from a known 360 camera) and unstitched Insta360 frames
holding two fisheye circles. The latter are stitched into a panorama for the
preview (and for the LLM): the two lenses are given the same brightness and
colour and blended where they meet (a hand-held stick is hidden by the
stitch already). A tripod's legs or a hand right below the camera can be
covered with a soft fill: set `nadir_fill_deg` in config.json (see
SETTINGS.md), for example to 6, to turn it on. It is off by default, as on clean ground it only
greys the ground. Thumbnails show a normal-looking view of the front of
the scene. Each 360-degree photo larger than its 2048 px preview also gets
a panorama up to 4096 px wide (made from the full-size original) so the
360 view stays sharp. Unstitched photos of an Insta360 ONE R are levelled
as they are stitched: the camera's motion sensor says which way was up
when the photo was taken, and the panorama is turned so its horizon is
level (see [Level horizons](#level-horizons); `level_horizon` turns it
off).

Coming from an older version? Read [Upgrading from an older
version](#upgrading-from-an-older-version) first.

## Install

You need Python 3.9 or newer and a few packages:

    python -m pip install -r requirements.txt

`pillow-heif` is only needed to scan libraries that contain HEIC images.
Without it the scanner still runs and skips HEIC files with a warning. Each
script also prints an install command if a package is missing.

Videos need the `ffmpeg` and `ffprobe` programs (from
https://ffmpeg.org/download.html or your system's package manager), on the
PATH or set with `"ffmpeg"` in `config.json` (see [Videos](#videos)).

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

To try it on the 19 bundled sample pictures first, see
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

The Settings page can start and stop `llama-server` for you: add a
`local_server` section to `config.json` (where the program and the models
folder are; see [local_server](SETTINGS.md#local_server) in SETTINGS.md)
and the **Model server** panel gets **Start** and **Stop** buttons, a
choice of the models in that folder, the slots and context, and the
server's log. Describing then starts the server when it is needed and
stops it again afterwards, and a daily stop time keeps the graphics card
free from that time on (jobs that use the server stop ten minutes before,
can be resumed later, and do not use it again until an hour after). A
server started any other way is used as it is (also while it is still
loading its model) and never stopped from the page. The helper module can
also start a server before each describe run (see
[helper](SETTINGS.md#helper)).

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

Hidden pictures (see [Searching](#searching)) are never sent to a cloud
service, or to any endpoint outside this computer and your network: not
for describing, refining or comparing models. They are skipped and
counted ("3 hidden pictures were not sent to api.openai.com"), and a local
endpoint (in the library's endpoint group, say) still describes them. The
Manage page does not count them as waiting to be described (or refined)
by such an endpoint: they show as "hidden, kept here". A `--limit` counts
only the pictures that can be sent. To keep whole folders away from a
cloud service, give their library a local endpoint, or skip them with
`ignore` patterns (see SETTINGS.md).

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
API key or an empty account stops the run with a message. A reply cut off
at the endpoint's length limit (`max_output_tokens`), typically in the
middle of a long transcription, is kept when it got as far as the title and
the description (its provenance says `truncated` and lists the fields it
did not reach); otherwise the picture is asked for again with twice the
room. A reply whose text went round in a loop until it was cut off, or
whose JSON broke off, is asked for again first, and only kept when the
retries do no better. A cut-off reply that did not get as far as the
private-information answer counts as showing private information, so
exports leave the picture out unless private pictures are included.

### Refining documents and screenshots

A small model describes photos well but may cut the text of a long
receipt, letter or chat screenshot short. A library can name a second,
stronger endpoint for those: its **refine** setting (**Edit** on the
Manage page, "Refine documents and screenshots"). The refine pass then
describes the library's pictures of the chosen kinds again (by default
screenshots, documents and scans, and receipts, letters, forms, ID cards,
tickets, chats, web pages, apps, handwriting, book pages and slides,
whatever kind of picture they are), with up to 8000 tokens for the answer,
and the new description replaces the old one. Start it with **Refine
documents** on the library's card (it shows how many pictures it would
do), or with `python scan.py --refine`. A picture refined with the current
prompt is not refined again, and regular describing never overwrites a
refined description (**Re-describe out of date** does after a prompt
change, and **Describe again** in the viewer does for one picture).
When a refine answer is cut off, the fields it did not reach keep their
earlier values, and it never clears the earlier description's private or
sensitive flags. Pictures whose refine failed three times keep their
earlier description and are skipped; **Retry failed refine** on the card
tries them again.

## Scanning

The Manage page's buttons and `python scan.py` do the same work. A scan
finds new and changed images and reads each of them once: it stores their
metadata, EXIF data, capture date, camera and GPS position in a SQLite
database (`data/index.db`), builds a thumbnail and a 2048 px preview (about
0.5 MB per picture in `data/previews/`; see `previews` in SETTINGS.md), and
then the LLM describes anything without a description. With [search by
meaning](#search-by-meaning) set up, the pictures whose description is new
or changed then get their meaning vectors. Run it again whenever your
folders change; unchanged files are not read again.

Descriptions follow the pictures: a file that is touched, renamed or moved
keeps its description, and an exact copy of a described picture gets the same
description. A file that disappears is first marked missing and only removed
after `purge_after_days` (30 by default), so it comes back as it was if the
file returns (a library on a share that is offline is skipped altogether). If
most of a library seems to have vanished at once, nothing is marked; run
`python scan.py --library NAME --allow-mass-removal` if that really was
intended.

When a new version stitches unstitched 360-degree photos differently (or
`nadir_fill_deg`, `zenith_fill_deg` or `level_horizon` changes), the next
scan stitches the ones in the index again (and the 360-degree videos):
their previews, panoramas and thumbnails are made anew, their descriptions
are kept. A scan stopped part way carries on with
the rest the next time.

### From the command line

    python scan.py

Options:

- `--no-describe` indexes without calling the LLM; `--no-scan` only
  describes.
- `--library NAME` limits the run to one library (repeat it for more).
- `--newest-first` / `--oldest-first` choose the order images are described
  in (by default each library's `newest_first` setting).
- `--retry-failed` also retries images the LLM failed on three times.
- `--redescribe-stale` also describes again images whose description is out
  of date (an older prompt, or a video description that talks about its
  frames, see [Videos](#videos)), after the new ones, and drives whose
  description is. Older descriptions stay searchable until then.
- `--limit N` describes at most N images in this run.
- `--endpoint NAME` describes with this endpoint (or group) instead of each
  library's own.
- `--refine` runs only the refine pass (see [Refining documents and
  screenshots](#refining-documents-and-screenshots)): no scan, no regular
  describing. `--library`, `--limit`, `--retry-failed` and the order options
  work with it.
- `--embed` runs only the embed step of [search by
  meaning](#search-by-meaning): the pictures whose meaning vector is
  missing or out of date (`--library` and `--limit` work with it). It
  exits with 1 when search by meaning is not set up or its endpoint
  cannot be used.
- `--embed-images` only makes the [looks](#looks-like-this-and-search-by-a-picture)
  of the pictures that have none (`--library` and `--limit` work with it;
  `--rebuild-looks` first throws away looks made with other settings, or
  after a failed check of the image model, the looks and the reference
  they are checked against; looks of the current settings stay, so a
  stopped rebuild goes on where it was).
  No other run makes them: they need the image model, best on a graphics
  card. It exits with 1 when looks are not set up or cannot be made.
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

## Videos

Videos are indexed like photos, when the programs `ffmpeg` and `ffprobe`
can be found: on the PATH, or where `"ffmpeg"` in `config.json` says (see
SETTINGS.md, "Videos"). Without them a scan lists the videos but does not
read them, and the Manage page says once how many were found and what to
do; videos indexed before stay as they are. A library's `videos` setting
(**Index videos** in its settings on the Manage page) turns them off.
Insta360 360-degree videos are indexed too: see [360-degree
videos](#360-degree-videos).

A scan reads each new or changed video once (frames are decoded on the
processor, never the graphics card):

- What ffprobe says: the length, the size as shown (a phone video filmed
  upright is upright), the codecs, frame rate and bit rate, the camera,
  the GPS position, and when it was taken. That is the QuickTime creation
  date with its time zone (iPhones and many other phones write it);
  otherwise the creation time in the file, which is in UTC, shown in this
  computer's time zone (as a photo's file time is) -- except when it is
  more than 15 minutes later than the file itself: apps that convert or
  export a video write the time they did that there. Then a date and time
  in the file name (`VID_20190514_142301.mp4`, `DJI_20190305_093012.MP4`;
  a name within 15 minutes of the creation time is used instead of it,
  as cameras that write when the recording ended name the file when it
  started); otherwise the file's time, as for a photo without a date (not
  shown on the timeline or in On this day). Dashcams and other cameras
  that know no time zone write their own clock's time as the creation
  time, marked as UTC, and name the file by the same clock
  (`20230817_142005_0412_N_A.MP4` created at 14:21:05 "UTC", when that
  one-minute clip ended): when the creation time, read as it is, is the
  name's time give or take two minutes (or its end: the name's time plus
  the video's length), the name's time is taken, with no time zone shown.
  (Phones write a real UTC time; their videos keep the rules above: never
  for a Pixel's names, which are in UTC, or a video with a phone's own
  tags.) Videos an older version dated hours off this way get the name's
  time on the next scan (**Scan** on the Manage page, or `scan.py`): it
  reads the dates of just those videos again (ffprobe only), once. A
  video that cannot be read because its file cannot (a share that went
  away for a moment) is tried again by the next scan, not marked as
  unreadable.
- A poster: the frame at 1 second (10 % into a shorter video), or a later
  one when that frame is black. It is the video's thumbnail and 2048 px
  preview, and finds look-alikes as a photo's picture does.
- A contact sheet for describing: four frames spread over the video, in a
  2 x 2 grid (`video_frames` in SETTINGS.md), kept next to the previews
  (`<id>.sheet.jpg`). A very short video's sheet may hold fewer frames;
  the model is told how many. HDR (HLG) videos are tone mapped to ordinary
  colours when ffmpeg has the zscale filter (the common builds do).
- A content hash made from the size and the first and last 4 MB, so that
  moved, renamed and copied videos keep their descriptions without reading
  every byte of large videos (on a share). A video changed only in the
  middle without changing its size would count as the same video.

Videos never stack with photos, and with other videos only when they are
the same file. The AI model gets the contact sheet with a note saying it
holds four frames, in order, of a video of so many seconds, and that the
title and description are about what happens in the video, never about
the frames or the grid, and that a video filmed with a camera is a photo
(even with the date, time or speed a dashcam prints on it) and only a
recording of a screen a screenshot. The kind of picture is always "video"
(the refine pass never picks videos), though what the model saw (a screen
recording looks like a screenshot) is still searched and still counts for
an export's **Leave out** choices. The model never hears a video's sound.
Hidden videos are never sent to a cloud service, as with photos.

Models still describe the sheet now and then ("Four sequential frames
show ...", "The top-left panel shows ...", "visible in each frame"). When
a new video's (or drive's) title or description does, the same endpoint
is asked once more, with the same picture and one more sentence saying
that it described the frames and is to describe the footage itself as
one continuous scene. That request waits out rate limits and server
errors like any other, but an unusable answer is not asked for again, so
there is one extra answer at most. The second answer is kept when it no
longer talks about the frames, otherwise whichever of the two does so
less (the first on a tie, and the first when the second was cut off and
the first was not); either way the description counts as asked again
("reasked" in its provenance) and stays as it is. When the second request
gets no answer at all (the service stays busy or fails), the first one
is kept and stays out of date. The run's log says how many were asked
again, how many that fixed and how many could not be asked again, and
the tokens include the extra request. Video and drive descriptions that
talk about their frames and were not asked again (made before this
check, or whose second request failed) count as out of date: **Re-describe
out of date** (or `scan.py --redescribe-stale`) describes them again.
`"video_sheet_check": false` in `config.json` turns all of this off (see
SETTINGS.md). The check reads English words, such as "four frames", "a
sequence of dashcam footage", "in the final frame", "in the first shot",
"the bottom two panels", "a split-screen" or "across the frames";
"across the frame", "in the frame", "out of frame", "picture frame",
"frame rate", "a sequence of events", "The final shot shows" and a
dashcam's time and speed "in each frame" are ordinary video words and
never count.

In the search page, videos have a play sign and their length on the
thumbnail (`▶ 0:24`), `is:video` (or `type:video`, or **Videos** in the
Type filter) finds them, as do the words video, movie and clip, and they
are on the map and the timeline like photos. The viewer plays them
(`Space` plays and pauses; `←` and `→` still change pictures, unless the
player has the keyboard focus, where they go 5 seconds back or ahead),
with the length and codec in the information pane. Browsers play H.264
videos; HEVC (most phones' videos of recent years) plays in Safari, and
in Edge or Chrome only where the computer can decode it. When a browser
cannot play a video, the viewer shows the poster and says so (on this
computer with **Download the original**). For those videos
`"video_proxies"` in `config.json` (off by default; see SETTINGS.md) adds
**Make playable copies** to the Manage page: H.264 copies in
`data/proxies/`, which the viewer then plays. A copy follows its video
when the file is moved or renamed, and goes when the video or the
library's indexed data is deleted.

### Drives

Dashcams and many action cameras cut what they record into short files, a
minute or a few each. A run of such clips is a **drive**: videos in the
same folder whose names follow the same pattern
(`20230817_142005_0412_N_A.MP4`, `20230817_142105_0413_N_A.MP4`, ...), of
the same size and kind (not 360 videos, nor any `.insv` file), each one
starting at most 20 seconds after the one before it ended (and not before
it ended), and each one but the last as long as the other clips of that
recording (at least 30 seconds): a shorter clip means the recording stopped
there. So videos started and stopped by hand one after another (a phone's
clips of one evening) make no drives, while the long segments a camera cuts
one recording into do, whatever their length. When the names count the
clips (most dashcams number them), the count must go on from clip to clip:
a drive also goes on over a few missing clips (deleted, or kept elsewhere
as "locked" event clips) when the time between fits them (the gap is a hole
in the drive), and two cameras of the same kind in one folder make drives
of their own. A drive has at least three clips. Drives are worked out from
the index whenever they are needed, so a scan that finds more clips at the
end of a drive makes it longer (it keeps its description until the next
describe run describes it again). `"drives"` in `config.json` changes the
20 seconds and the three clips (see SETTINGS.md).

- The search page shows each drive as one tile, a stack with its whole
  length (`▶ Drive 1:04:30`) and the number of its clips (`×65`), which
  opens them in the grid, in order (in the viewer, **Show its clips in the
  grid** does the same). A drive holds the clips the search may show: not
  hidden, sensitive or private ones the options leave out, and in the
  search of an album (`album:`) only the album's clips, as an export of the
  album has them. **Fold drives** next to **Group similar** shows the clips
  one by one instead (it shows only where there are drives). The clips stay
  searchable: a search that finds a clip shows its drive, as that clip, and
  the clips that match are ringed when the drive is opened. With nothing
  searched, a drive shows as its middle clip (its first is often the car
  still parked). `is:drive` finds the clips of drives, and the word drive
  finds them too. Selecting a drive's tile (**Select**) selects the clips
  that match the search (all of them when the drive matched by its own
  description), in the drive's order, and a smart album, **Copy to a new
  album** and the album's count take a drive found by its description with
  all of its clips.
- The viewer plays a drive clip after clip (the next one loads while one
  plays), with a strip under it across the whole drive: its clips (those
  that match the search in amber, a red edge after a hole: clips the camera
  made that are not there, also hidden ones, ones an export left out and,
  in an album, those not in it), how far it has played, and where it is
  ("Clip 12 of 65 · 11:04 / 1:04:30"). Click the strip to go there; with
  the focus on it, `←` and `→` go 10 seconds back or ahead, `Page Up` and
  `Page Down` a clip, `Home` and `End` to the start or the last clip.
  `Space` plays and pauses. The information pane follows the clip that
  plays, under the drive's own description, and so do the star and rating
  above the picture and their keys (`F`, `1`-`5`, `0`, `A`): they mark the
  clip on show. A clip of a drive plays the whole drive this way, folded or
  not. Clips a browser cannot play (HEVC) play once they have playable
  copies (`video_proxies`); until then, and for clips an export publishes
  as stills only, the viewer shows a clip's still with a note for a few
  seconds and goes on with the next one.
- Describing (the end of every describe run over whole libraries) also
  describes each drive once, from nine frames spread evenly over the whole
  drive in a 3 x 3 grid, with a note that they come, in order, from a
  drive of so many minutes made of so many clips. The description is kept
  in `data/drives.json`, shown with the drive and searched as the drive's
  own words, by words and by meaning: a search that matches them finds the
  drive (as its middle clip, unless a clip matches by itself). A drive
  with a hidden clip is only sent to endpoints on this computer or the
  local network, and its description is yours alone: it was made from
  that clip's frames too, so visitors, exports and the meaning vectors
  never get it (as for its sped-up preview). One the model flagged as
  showing private information or as sensitive is searched and shown only
  where such pictures are.
- Sped-up previews, off by default (`"drive_previews"`, see SETTINGS.md):
  **Make sped-up drives** on the Manage page writes an H.264 copy of each
  drive, 60 times faster (an hour in a minute) and 720 pixels high,
  without sound, into `data/drive-previews/`. The viewer then offers
  **Sped up ×60** next to the strip, and plays it by itself when the clips
  cannot be played in the browser. Only the key frames of the clips are
  decoded when they come often enough (most dashcams: two a second), so a
  preview is made about as fast as the clips can be read; it takes some
  tens of MB per hour of driving.
- An export keeps the drives of the clips it publishes, with a hole
  where clips are left out (as long as what is missing there), and their
  descriptions unless its options leave out what they are flagged as
  (private information, sensitive) or a clip of the drive is hidden; the
  review step says how many keep theirs. It has no sped-up previews.

#### Routes (a dashcam's GPS)

Many dashcams record the fixes of their GPS receiver into their videos,
one a second (the position, speed and time their overlay prints over the
picture). A scan reads them with the video -- only the small blocks that
hold them, which the camera lists, so a clip on a share costs a fraction
of a second, and no program runs (other videos: their first 4 MB, looked
through once) -- and keeps each clip's **track**, simplified so that the
place at any moment of the clip is still known to within 10 m (a minute
of driving takes about a hundred bytes). Videos indexed before are read
the same way by the next scan. Read are Novatek-style `freeGPS` blocks
(with an NMEA RMC sentence, or the same fields as numbers in one of the
layouts exiftool documents; other cameras' blocks -- encrypted ones, other
layouts -- are not) and NMEA RMC sentences in a text (subtitle) track;
fixes the camera marked as not valid, impossible dates, glitches (a time
or a position far from the fixes around it) and repeats are left out.

- A clip with a track gets the position of its middle fix: its place
  names, `has:gps` and a pin on the map, like a photo with GPS (the viewer
  says "from the video's GPS track"). When its file names no time zone (a
  dashcam's clock knows none), it gets the UTC offset its clock has by the
  GPS time (`-08:00`), when that is a whole quarter hour (give or take a
  minute); its time stays the camera's clock. The scan's log says what
  offsets the clocks have. That offset is the camera's clock's -- a
  dashcam's often ignores daylight saving -- so only the same camera's
  pictures (and the clips of its folder) go by it, never the library's
  other pictures.
- The GPS check times such a clip by its GPS time and counts its position
  as a strong one; a clip without a fix in a folder of clips whose clocks
  keep time (the camera was still looking for satellites, or stood in a
  garage) is timed by their clock. The points of a track, every 10
  seconds, count as pictures taken there and then for the [estimated
  places](#estimated-places): a picture taken on the way is placed by the
  track, and a clip without a fix where the car stood.
- The text the AI model read off a dashcam's overlay is never taken as a
  position: when the camera has no fix it goes on printing the last one it
  had (for minutes, in a garage), and the model misreads digits.
- A drive has a **route**: the tracks of its clips one after the other,
  with a gap where clips are missing (a hole) or have no fix. The **Map**
  draws the routes of the clips the search finds as lines while **Show
  drive routes** is on; the pointer over a line says the drive, the clip
  and how far into it, and a click (or a tap) plays the drive from there.
  The **viewer** shows a drive's route in a small map in its pane (on a
  phone right under the clip's title, in sight with the video), with a
  dot where the clip that plays is, and the speed there on the strip
  ("Clip 12 of 65 · 11:04 / 1:04:30 · 52 km/h"); neither while the clip
  had no fix (before the first, after the last). Both need nothing but the
  map's tiles, so they work offline and in an export.
- Routes and the clips' positions are location data. Other computers see
  them as they see positions (never those of hidden clips); an export
  publishes them only with **Location** and **Drive routes**, and leaves
  out every point within 500 m of where a drive (of any library) starts or
  ends (see [Publishing a library](#publishing-a-library)). It never
  publishes the positions written in a dashcam clip's texts. What the
  overlay prints over the clip's picture it cannot take out: the review
  says how many clips are filmed near where a drive starts or ends, or
  have no fix (their overlay then shows the last position the camera had,
  often where the drive started) -- hide those to keep that place
  private.
- `"gps_tracks": {"enabled": false}` in `config.json` stops reading
  tracks, and the next scan takes away the positions they gave (they come
  back when it is on again).

To list the drives found, without changing anything (`--sheet FOLDER` also
writes the 3 x 3 sheets the model sees):

    python drives.py [--library NAME] [--sheet FOLDER]

To see the GPS track read from videos, without changing anything:

    python tracks.py path/to/clip.mp4 [--fixes]

To see what is read from one video, without changing anything:

    python videos.py path/to/video.mov

### 360-degree videos

Insta360 cameras save a 360-degree video as
`VID_<date>_<time>_00_<number>.insv`. The X4 keeps both lenses in that one
file (an MP4 with the picture of each lens in a stream of its own, HEVC,
which no browser plays as such); the ONE R (with its 360 lens) writes the
second lens to a file of its own, `VID_<date>_<time>_10_<number>.insv`.
Next to them the camera writes a small H.264 copy with both lenses side by
side, the LRV: `LRV_<date>_<time>_01_<number>.lrv` (the ONE R names it
`LRV_<date>_<time>_11_<number>.insv`). A scan indexes the first `.insv` as
the video and pairs it with its LRV and second lens's file: the ones in the
same folder with the same date, time and number. Those are never indexed
on their own.

The lenses of each camera's videos are measured, so only those of the X4
and the ONE R are stitched; the videos of other Insta360 cameras are
indexed as ordinary videos (the picture of one lens), as are videos whose
camera the file does not name.

- The poster and the contact sheet are made from the LRV's frames (without
  one, from the two lenses put side by side, which takes some seconds per
  video on the processor) and stitched into equirectangular panoramas the
  way unstitched 360 photos are: the same lens model (with values for each
  camera's videos), the same blend at the seams, the same evening out of
  the two lenses and the same `nadir_fill_deg` and `zenith_fill_deg`. As
  for a 360 photo, the preview is the panorama, the thumbnail (and the
  picture "Looks like this" compares) its front view; the size shown is
  the full panorama's (7680 x 3840 for two 3840 px lenses). The model gets
  the four stitched frames with the video note and a note that each frame
  is a 360-degree view.
- The camera writes its own clock's time in the file name and the creation
  time in UTC: the name's time is when it was taken, and the difference is
  the time zone (`UTC+02:00`), so the videos sit among the camera's photos
  on the timeline. (No difference says nothing: older cameras write their
  clock's time as the creation time, so no time zone is shown.) The
  camera's model comes from the end of the file ("Arashi Vision Insta360
  X4", as its photos name it).
- When an LRV or a second lens's file is added or deleted later, the next
  scan reads the video again (its description is kept).
- The viewer shows the poster in the 360 view at once; **Play** (or `Space`)
  plays the LRV on the sphere: each frame is stitched as it is shown, by
  the graphics card (WebGL), with the same lens model and seam blend as the
  poster, so nothing is converted or stored. Drag to look around while it
  plays; the bar under the view has play and pause, the time, a bar to move
  in the video, **Level** (see below) and the sound. Without WebGL (or in
  an export opened from disk, where browsers do not let the 360 view read
  the video) it plays flat: the circle of the front lens.
- A 360 video without its LRV plays once `video_proxies` is on and
  **Make playable copies** has run: its copy holds both lenses side by side
  (as an LRV does), at the `height` of that setting. Until then the viewer
  shows its poster in the 360 view and says so. (A video that holds both
  lenses in one H.264 stream plays as it is.)
- An export with videos included publishes the LRV (or that copy), without
  its metadata, up to `max_video_mb` (an LRV counts as what its copy keeps:
  not the camera's data at its end); the site's viewer plays it on the
  sphere the same way (from a web server), levelled as below. Without it,
  the site shows the poster in the 360 view.

#### Level horizons

A camera held tilted, low on a stick or flat over something makes a
panorama whose horizon is a wave and whose trees lean. The X4 and the ONE
R record their motion sensor (accelerometer and gyroscope, about 500 to
1000 readings a second) at the end of each video and photo, and that says
which way was up at every moment:

- Reading a 360 video works out the direction of up in its panorama over
  the video's time (its "level track": the accelerometer, which feels
  gravity, averaged over 4 seconds after the gyroscope's turns are taken
  out, so the camera's own jolts do not count and quick turns are
  followed). Only the tilt is taken out: the middle of the panorama still
  looks where the camera looked (a camera upside down, as on a hanging
  mount, keeps the heading of its front). A timelapse (TimeShift) plays
  far faster than it was recorded: the camera notes when each of its
  frames was taken, and each is levelled for that moment. Where the
  readings stop before the video ends, the turn fades out rather than
  holding a tilt that may no longer be right. The track is kept with the
  video's facts: up to about 15 KB per minute of hand-held video, a few
  hundred bytes for one on a tripod.
- Its poster and contact sheet are turned level with it, so the
  thumbnail, the AI model and "Looks like this" see a level picture (a
  picture stitched again has its looks made again by the next **Update
  looks**).
- The viewer turns every frame level as it plays (a small turn per frame
  on the graphics card, at the frame's own time). **Level** among the
  controls turns that off and on (remembered in the browser); it shows
  only for videos that have a track.
- Unstitched ONE R photos (`.insp`) are levelled with the sensor's
  readings at the moment they were taken (also those taken in quick
  succession, whose files name the moment of the photo before). The X4
  levels the 360 photos it stitches itself, so those are shown as they
  are.
- A published 360 video takes its level track along (which way was up
  over time: no place or date).

The setting `level_horizon` (on by default) turns all of this off: the
next scan stitches the posters and photos again as the camera saw them,
and the viewer plays the frames that way. Cameras whose sensor axes are
not known here are not levelled.

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
  place, camera, tags, subjects, animals, food, vehicles and colors (a tag
  and its plural, such as "dog" and "dogs", count as one, shown the way it
  is written most often). **Place** lists countries; choosing one shows
  its regions under it, and choosing a region its towns (see [Place
  names](#place-names)).
  Values of one filter are alternatives, different filters must all match;
  the chosen ones show above the results with a button to remove each.
  "Hide blurry and accidental shots" and "Hide sensitive pictures" (on by
  default: explicit and graphic pictures) are there too.
- **The timeline** above the grid counts the results per year (per month
  when they are all of one year, per day when they are all of one month).
  Click a bar to see only that year (month, day): the timeline then shows
  its months (days). For several, drag across them, or hold Shift and
  click (or press `Enter` on) the first and then the last (`Esc` forgets
  the first). The dates show as a "Taken" chip above the results, **Zoom
  out** widens them again, and Back goes back. The arrow keys move between
  bars. The timeline folds away with its title (and stays folded). It
  counts the dates pictures were taken, from the camera or the file name:
  a picture dated only by its file's time (a copy, a download) counts as
  "without a date" there and in **On this day**, though sorting by date
  still uses that time.
- **On this day** (in the page header; a calendar on a phone) shows the
  pictures taken on today's date in earlier years, with the years ago
  above each year's pictures (the search `on:today`).
- **Surprise me** opens a random picture of the results (of everything
  when nothing is searched). The **Shuffle** order puts the results in a
  random order that stays the same while you scroll and in that browser
  tab (choosing Shuffle again shuffles anew).
- **Group similar** shows near-duplicates (and exact copies) within each
  library as one stack with a "x3" badge; click the badge to see them all
  (copies in two different libraries show separately). The stack shows its
  best picture: one you marked (a favorite, then the higher rating, then
  one in an album; in an export only with its **marks** option), then the
  best looking and sharpest, then the largest,
  then an original (with a camera name and the earliest date) rather than
  a copy. Pictures whose camera (or file name) dates them are only stacked
  when they were taken the same day (or within 12 hours across midnight),
  and then rather with each other than with a look-alike dated only by its
  file; pictures dated only by their file need a closer match. Shots of a
  burst or an exposure bracket (taken within two seconds) may differ a
  little more. Pictures of clearly different shapes (a photo and a
  panorama, 4:3 and 16:9, or a landscape and an upright picture: more than
  15% apart) are only stacked when they are identical files. Documents and
  screenshots are only stacked with exact copies, as different pages look
  alike to the comparison. With [looks](#looks-like-this-and-search-by-a-picture)
  (made by an image model), stacks also go by what the pictures look like:
  see [Duplicates by look](#duplicates-by-look).
- **The viewer** opens when you click a thumbnail: the picture at screen
  size, arrow keys (or swiping) for the next and previous result, zoom with
  the wheel, a pinch or a double click, and the information pane (`i`):
  date, place, camera settings, the description and text in the picture
  with your search words marked, clickable tags and other facts (each
  starts a search, the place too), quality, and "More like this": a strip
  of the closest pictures, and a **More like this** button (or `M`) that
  searches for all of them (see [Search by meaning](#search-by-meaning)).
  360-degree photos can be looked around in by dragging (the flat
  panorama when the browser cannot show that), and so can 360-degree
  videos while they play; they open facing what they show (see [Where 360
  pictures open](#where-360-pictures-open)), and **Reset the view** (`0`)
  returns there.
  "Details page" opens the same on a page of its own, to link to.
- **On a phone** the information pane is a sheet at the bottom: opened
  (`i`), it shows the title and the key facts, and the picture (with a
  video's controls, a drive's strip) fits in the space above it. Drag its
  handle up (or tap it) for the rest, down to close it; the handle also
  takes the up and down arrow keys. A phone held sideways shows a narrower
  pane beside the picture instead, closed until you open it. On touch
  screens the controls are at least 40 pixels to the finger (links within
  a sentence and the timeline's bars aside).
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
  "Select all") to hide, unhide, add or remove tags, or copy their file
  paths.
- **Your own words** (on this computer too): **Edit** in the viewer (or
  `E`) changes a picture's title, description and tags; `Ctrl+Enter`
  saves, `Esc` cancels, and "Use the model's ..." puts the AI model's text
  back. Tags you add are the way to teach the search a name, such as a
  pet's or a place's: they are searched like the model's tags, show first,
  and are marked "added by you". Edited pictures show an "Edited" badge
  with the model's own text folded away under it, and `is:edited` finds
  them. **Add tag...** and **Remove tag...** on the selection bar do the
  same for many pictures at once. Your edits are kept apart from the
  model's description: describing a picture again never changes them.
  They belong to the picture, not to its file name: a file moved or
  renamed (in its library or to another one) keeps them, and when a new
  picture is saved under an old name (cameras reuse names) the edits stay
  with the old picture at its new place. They go when the picture is
  removed from the index, unless a copy of it is still there. Published
  exports and other computers see your words (never the model's text you
  replaced, nor which pictures you edited).
- **Favorites, ratings and albums** (on this computer too): the star in the
  viewer (or `F`) makes a picture a favorite, the five small stars (or the
  keys `1` to `5`) rate it, `0` clears the rating, and **Add to album...**
  (or `A`) puts it in an album; see [Favorites, ratings and
  albums](#favorites-ratings-and-albums).
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
  pictures); a static export with the map works the same way. Estimated
  places are hollow pins, shown while **Show estimated places** is on (see
  [Estimated places](#estimated-places)). The routes of drives are lines,
  while **Show drive routes** is on: point at one to see the clip there,
  click it to play the drive from there (see [Routes](#routes-a-dashcams-gps)).
  The map uses
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

A few common words also find their synonyms, below the word itself (a
picture tagged `mom` comes before a "mother duck"): `kids` finds children
(and toddlers, boys and girls), `car` automobiles, `plane` airplanes and
aircraft, `sea` the ocean, `mom` mothers, `couch` sofas, and so on. A
broader word finds the narrower ones but not the other way round (`dog`
finds puppies and retrievers, `puppy` only puppies), and so does a word
with other meanings (`fall` finds autumn pictures, but `autumn` finds no
waterfalls). Quoted words, the text in pictures and field filters other
than `tag:` and `animal:` are never widened. For animals, vehicles and
food (and flowers and plants), the real thing ranks above pictures of it:
a dog the model listed among the picture's animals (or named in its
title) comes before a stuffed dog, a dog statue, a drawing or a logo, a
hot dog, and a menu or a screenshot that mentions one -- unless you ask
for those (`toy car`, `dog statue`, `bird art`, `car document`), which
then come first. `animal`, `vehicle` and `food` put pictures with real
ones first. When you search for one word, pictures that have it only in
the text in the picture (a sign, a page) come after the pictures that are
about it.

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
  (also a video's from its GPS track)
  (also `has:people`, `has:animals`)
- `on:today` - taken on this day in earlier years; `on:05-14` - on May 14
  of any year; `on:2019-05-14` - on that day (dates from the camera or the
  file name only, not a file's time)
- `place:hawaii`, `place:"new york"` - taken in a town, region or country
  (see [Place names](#place-names)); place names are also found as ordinary
  words (`beach hawaii`), and a few other names of countries too (`usa`,
  `america`, `uk`, `britain`, `holland`, ...; `us` alone or as `US`: next
  to other words a lower-case `us` is skipped like "me"). A word found only
  in the place names ranks below the same word in what a picture shows: a
  room in Long Beach comes after the pictures of beaches.
- `is:360`, `is:duplicate`, `is:undescribed`, `is:edited` - 360-degree
  photos and videos, pictures with near-duplicates in the same library,
  pictures not described yet, pictures whose title, description or tags you
  edited
- `is:video` (or `type:video`) - videos; `is:photo` - photos (not videos,
  screenshots or documents); `is:drive` - the clips of drives (see
  [Drives](#drives))
- `is:gps-ignored` - pictures whose GPS position the GPS check set aside
  (on this computer only; see [the GPS
  check](#positions-that-cannot-be-right-the-gps-check))
- `is:place-estimated` - pictures without a GPS position of their own whose
  place is estimated (see [Estimated places](#estimated-places))
- `lib:name` - only the pictures of one library
- `is:favorite` - your favorites; `rating:4` - rated 4 stars,
  `rating:>=4` (or `rating:4+`) 4 or 5 stars, `rating:<3` 1 or 2,
  `rating:2-4` 2 to 4, `rating:0` not rated (`is:rated`, `is:unrated`)
- `album:"Summer trip"` - the pictures of an album (its name in any case,
  or the start of it when only one album starts so; quotes when it has
  spaces; `album:#12` the album with id 12, which is what the albums'
  links use when a name could mean another album). Alone it shows the
  album in its own order.
- `similar:1234` - the pictures most like picture 1234, closest first (what
  **More like this** searches; see below)
- `look:1234` - the pictures that look most like picture 1234, closest
  first (what **Looks like this** searches; see below)

### Search by meaning

Words find the pictures that have them (or a synonym). With an embedding
model set up, the search also finds pictures that mean what you typed:
`kids playing in the snow` finds a picture described as two children on a
sled, `papers about a car repair` an invoice from a garage. Each described
picture (and video: its vector says it is a video) gets a meaning vector,
made from its title, description, tags and the other things its
description says (never its file or folder name, dates, places or
camera), by a small embedding model on this computer, run by llama.cpp's
`llama-server --embeddings` (or Ollama, LM Studio, ...). The recommended
model is MongoDB's mdbr-leaf-ir: small enough for a CPU (the whole
library in a few minutes, a few milliseconds a search, no graphics card)
and for a published site, where it lets typed searches find pictures by
meaning too (see [Publishing a library](#publishing-a-library)). It
comes as three files: the model (a Q8_0 GGUF file), its Dense head (a
last layer llama.cpp leaves out, applied by these tools) and its license
text. Add an endpoint for it and name it in `config.json`:

    "endpoints": [ ..., {"name": "embed", "preset": "llamacpp",
                         "base_url": "http://127.0.0.1:8099/v1"} ],
    "embedding": {"endpoint": "embed",
                  "model_file": "C:\\path\\to\\models\\model.gguf",
                  "dense_file": "C:\\path\\to\\models\\model.dense.safetensors",
                  "license_file": "C:\\path\\to\\models\\model.LICENSE.txt",
                  "credit": "mdbr-leaf-ir by MongoDB (Apache-2.0)"}

start the server (for example `llama-server -m model.gguf --embeddings
--host 127.0.0.1 --port 8099 -c 512 -ub 512 -b 512 --device none -t 4
--cache-ram 0` (an older llama.cpp that does not know `--cache-ram` works
without it), or let a `local_server` entry with `"kind": "embeddings"`
start it when needed; see [SETTINGS.md](SETTINGS.md), which also says
how to use a larger model such as Qwen3-Embedding), and press **Update
meaning search** on Manage (or run `python scan.py --embed`). From then on scans,
describe and refine jobs keep the vectors up to date: only pictures whose
description changed (a new description, the refine pass, your own edits)
are embedded again. Until a picture has its vector it is found by its
words only; so is a picture you edit, from the moment the edit is saved
until the next scan, describe or refine job (or **Update meaning
search**) embeds it again, so no search by meaning finds it by words you
took out. When that embed step at the end of a job finds the embedding
server not running, it says so once and leaves the work for later.

A switch next to the sort order chooses how a search goes:

- **Words + meaning** (the default): the pictures with your words come
  first, ranked by how well both their words and their meaning match;
  after them come up to 40 pictures without the words that mean much the
  same, marked with a small "≈" ("Found by meaning"). The count above the
  results says how many were found by meaning; when none has your words
  (a typo, say) a line says so and offers a spelling that has matches.
  Names, places, years, file names and words read in a picture mean
  little to the model: when the pictures with such a word are not what
  the model makes of it, the search goes by the words alone.
- **Meaning**: by meaning alone, closest first, words or not; a line
  above the results says how many pictures have the words.

Close in meaning means much closer than the other pictures are; in a
small set of pictures (a small published library, say), where none can
stand out much, the closest few still show when they are a little closer
than the rest.
- **Words**: the words alone, as without an embedding model. The page
  remembers this choice; the other two last until you leave the page.

Filters, the timeline, quoted phrases, field words (`tag:dog`) and
`-words` stay strict in every mode: they apply to the pictures found by
meaning too. The meaning of what you type is asked of the embedding model
a moment after you stop typing (the same search again is remembered), so
the meaning matches join the word matches a moment later. While the model
is starting, or when it cannot be reached, the search goes by the words
and says so above the results. The switch only shows when the pictures
have vectors and the server can embed searches (and does nothing for a
search of filters or quoted phrases only).

**More like this** (in the viewer, `M`, and on the details page) searches
`similar:<picture>`: the pictures closest in meaning to that one, best
first, after the picture itself. Close means much closer than the other
pictures are; when few stand out like that (a small published library,
say), the closest few still show if they are close enough. It needs no
model, only the vectors, so
it also works in a published site (see [Publishing a
library](#publishing-a-library)). The viewer's strip shows the closest
few, one per stack of duplicates. A picture without a vector (not
described yet) finds pictures sharing its tags, scene or time instead.

The page downloads the vectors once (about 256 bytes a picture, and again
only when they change) and compares them in the browser, as it does the
words. Unlike the rest of a search, what you type then goes to the
server (`/api/embed`), and from there to the embedding endpoint: one on
this computer or network unless `allow_cloud` is set (see SETTINGS.md).
The server's request log leaves the words out, the browser does not
cache the answers, and each computer only ever gets back its own
searches. Vectors are kept in `data/vectors.db`; deleting it is harmless
(**Update meaning search** makes it again). Deleting a library's indexed
data deletes its vectors too.

### Looks like this and search by a picture

With an image embedding model set up (`image_embedding` in
[SETTINGS.md](SETTINGS.md); for example EmbeddingGemma 2 with its image
projector, run by llama.cpp's `llama-server`), every picture also gets a
look vector: what the picture itself looks like, made from its preview
(a 360-degree photo from the front view its thumbnail shows, a video from
its poster frame), not from its description. **Looks like this** in the
viewer (`L`, next to **More like this**) and on the details page searches
`look:<picture>`: the pictures that look most like that one, best first,
after the picture itself, with a strip of them under **More like this**.
It finds what descriptions miss (the same kind of light, the same view,
the same pattern) and only partly overlaps **More like this**, which goes
by meaning. Words, filters and the timeline still choose among them
(`look:123 year:2019`). It needs no model at search time, so it works in a
published site too (the export's `looks` option).

On your server's search page (on this computer), you can also drop a
picture anywhere on the page, paste one (outside text fields), or choose
one with the picture button in the search box: the server's image model
makes its look vector and the pictures that look like it are shown, with
a "Looks like: [picture]" chip above them that takes it away again. JPEG,
PNG, WebP, HEIC, AVIF, GIF, BMP and TIFF pictures can be used (up to 20 MB;
a 360-degree photo is compared by its front view, as the library's are).
Until the server has compared a new picture, the results stay as they
were, under a "Comparing: [picture]" chip; then the words in the search
box go (the filters stay), and words typed afterwards choose among the
pictures that look like it. A picture that cannot be compared leaves the
results as they were, with a message. The picture is sent to your own
server only and is never kept (not on disk, not in a log, not in the
address); while the image model starts, a line above the results says so
and the picture is compared a moment later.

Making the look vectors is slow on a CPU (a few seconds a picture, so
hours for a large library) and quick on a graphics card: no scan does it.
The Manage page says how many pictures have their looks and offers
**Update looks (N)** (or run `python scan.py --embed-images`); a scan's
log and its line on the Manage page say how many wait. Copies of a
picture share one look vector, and a moved or renamed picture keeps its
own; deleting a library's indexed data deletes the look vectors no other
picture shares. The vectors live in `data/image_vectors.db`: deleting it is
harmless but costly, as making them again is another long run. Changing
the image model, its `dims` or `max_edge` keeps the old looks working for
**Looks like this**, but pictures dropped on the page can no longer be
compared with them until you choose **Rebuild looks** on the Manage page
(it asks first, as it makes them all again). Every run first checks the
image model against vectors it made before (see `probe_check` in
[SETTINGS.md](SETTINGS.md)); when they differ, nothing is made, the run
says why, and the Manage page offers **Rebuild looks** for when the
earlier vectors were the wrong ones.

### Duplicates by look

With look vectors, **Group similar** and the Duplicates page also go by
what the pictures look like, not only by their dHashes (a small picture
fingerprint that a crop, a filter or the next shot of a burst changes).
How close two looks are is a cosine from 0 to 1 (1: the same look):

- Two pictures taken at most a minute apart (both dated to the second by
  the camera or the file name) stack when their looks have a cosine of at
  least 0.985, however different their dHashes are: bursts, the same view
  taken again, copies and edits made the same moment. Taken up to ten
  minutes apart they must look the same (0.99, as for **Same picture, any
  date** below); taken further apart, only their dHashes can stack them.
  On a varied library, pairs that close a minute apart were nearly always
  one shot or the same view again, while a few minutes apart half of
  those from 0.985 to 0.99 were other shots of the subject (the frog from
  the side, the next patch of leaves). A stack joins another whole, and
  only its first picture's look counts, so a long series does not chain
  into one stack; a single picture that is nearly the same as one
  picture of a stack taken within two seconds (the next frame of a
  burst) joins that stack. The shape rule and **Not duplicates** still
  apply, identical files always stack, and pictures without a time to
  trust (no camera date, or a date without a time) never stack by their
  look.
- Two pictures whose looks are clearly different are not stacked however
  close their dHashes are: a cosine of at most 0.85, or at most 0.93 when
  they were taken more than a minute apart (coins, casino chips or the
  sides of a mug photographed one by one on the same cloth). Every
  picture of a stack is compared, not just its first, so a picture kept
  out of one stack does not bring two different things together in
  another. Alike things photographed within one minute (or without times
  to compare) may still stack: true bursts of a dark scene came out as low
  as 0.86, and a black-and-white copy of a photo at 0.91.
- Only ordinary pictures go by their looks: documents, screenshots and
  videos stack only with identical files as before (two clips of a drive,
  or a 360-degree video's poster and a 360-degree photo of the same spot,
  look alike without being the same), and 360-degree photos keep to their
  dHashes, as their look is that of their front view only. Pictures with
  little detail (a dark or plain sky picture), which their dHashes cannot
  tell apart, may stack by their looks.
- **Same picture, any date** on the Duplicates page finds pictures whose
  looks have a cosine of at least 0.99 whatever their dates (a picture
  saved twice, an edited copy, the next frame of a burst) and lists those
  the search page does not show as one stack. It compares the pictures of
  the library shown, or of all libraries with **All libraries** (a copy
  kept in two libraries; each picture then says which library it is in).
  On a library with few copies saved again on other days, most of what it
  lists are near-identical shots of one moment that the search page put in
  two stacks, and series without a date to go by.

The cut-offs are the same for every library: near duplicates come out at
about the same cosine everywhere, and a cut-off worked out from each
library would change when pictures are added or hidden. They were set for
the default image model (see [image_embedding](SETTINGS.md#image_embedding)).
The groups are worked out again when looks are added or rebuilt (the
search page then gets its index anew). Neither the stacks nor **Same
picture, any date** compare every two pictures: the stacks compare a
picture with those taken about when it was, and **Same picture, any
date** only with pictures whose looks share enough of their signs, so both
stay quick on large libraries.

### Favorites, ratings and albums

On this computer (where the Manage page works) you can mark pictures and
put them together:

- **In the viewer**, the star at the top (or `F`) makes the picture a
  favorite (again: takes the star away), the five small stars beside it
  (or the keys `1` to `5`) rate it, and `0` clears the rating (when the
  picture is zoomed in, or a 360-degree view is turned, `0` first puts
  the view back). The information pane shows the same under the title,
  with the albums the picture is in (each opens its album) and **Add to
  album...** (or `A`): type to find an album and press `Enter` to put the
  picture in it (`Enter` never takes a picture out, and makes a new album
  only when no album matches what you typed; **New album** makes one
  whatever is listed), or click an album (clicking one the picture is in
  takes it out again). The details page works the same way.
- **Select** on the search page does the same for many pictures at once:
  **Favorite** (when all of them are favorites already: takes the star
  away), **Rate...**, **Add to album...** and **Remove from album...**.
  Favorites carry a small star in the grid.
- The **Favorites** and **Rating** filters count them, and `is:favorite`,
  `rating:` and `album:` find them (see [Search syntax](#search-syntax)).
- **Save as album** above the results keeps the search shown -- the words,
  the filters, the library, the timeline's dates, the sort and Words /
  Meaning -- as a **smart album**: it always holds the pictures that search
  finds now, so new pictures join it by themselves. Saving under the name
  of a smart album that exists changes its search. (Hiding blurry or
  sensitive pictures alone is not a search to keep.)
- **Albums** (in the page header) lists the albums with a cover, how many
  pictures each shows and **Open** (a search of the album). A manual album
  shows in its own order; **Arrange** changes it: drag the pictures (or use
  the buttons on each one), make one the cover, or take one out (with
  **Undo** right after). From the keyboard the arrow keys go from picture
  to picture, `Alt` and the arrow keys move one (`Alt+Home`, `Alt+End`: to
  the start, the end), `C` makes it the cover and `Delete` takes it out.
  **Rename** and **Delete** (its pictures stay in the library) are there
  too, and for a smart album **Change its search** (its search on the
  search page, where **Save the album's search** then updates the album,
  or saves it as a new one) and **Copy to a new album**, which makes a
  manual album of the pictures it finds now (an export can be limited to
  that one).
- The **Duplicates** page shows your marks on each picture, and the
  picture you marked (a favorite, then the higher rating, then one in an
  album) is the best of its stack; **Keep the best** never hides a
  picture you marked. On the search page, a stack shows that picture too
  (in an export only with its **marks** option; without it, the stack
  shows its best by quality).

Marks and album places belong to the picture, like your own words: a file
moved or renamed (in its library or to another one) keeps them, two files
that swapped names swap them, and they go when the picture is removed from
the index (a copy of it left in a library takes them; a favorite or
rating that no copy can take, because each has its own, is noted in the
scan's log). Other computers (a server started with `--host`) see the
stars, the ratings and the albums that hold pictures they can see, and can
open them, but never change them. A published site shows none of it
unless its `marks` option says so (see [Publishing a
library](#publishing-a-library)).

### Place names

Pictures with a GPS position can be found by the town, region and country
they were taken in. The place names come from
[GeoNames](https://www.geonames.org/) (the towns of the world with 1,000
people or more, under the Creative Commons Attribution 4.0 licence); they
are not part of this program. Download them once with **Download place
names** at the foot of the Manage page, or with

    python make_places.py

Either fetches three files (about 10 MB) from geonames.org into
`data/places/`, builds the place index `data/places/places.db` from them,
and then looks up the place of every picture with a GPS position. That is
the only time anything goes over the network for place names: looking up a
place happens on this computer, and nothing about your pictures is sent.
`make_places.py --from DIR` builds the index from the files downloaded by
hand (`cities1000.zip`, `admin1CodesASCII.txt` and `countryInfo.txt` from
https://download.geonames.org/export/dump/), `--redo` looks up every
picture again, and `--lookup LAT LON` prints the place of a position.

A picture's place is the town nearest to its position, by great-circle
distance: "Kahului, Hawaii, United States (3 km)" in the viewer. When the
nearest town is more than 50 km away, only its region and country are
kept (out in the country, or off the coast); when no town is within
250 km (the open sea), the picture has no place. Neighbourhoods are not
used as towns, so a picture in a city is in that city. Scans look up the
places of new and changed pictures as they read them, and of pictures
scanned before the place names were there afterwards; **Add place names**
on Manage does it at once, for every library (also those turned off).
**Download again** fetches newer files and looks up every place again; a
download that arrives incomplete is not used, and the place names stay as
they were.

Place names are words about where a picture was taken: other computers
see them like the GPS positions (see [Searching from other
computers](#searching-from-other-computers)), and an export publishes them
only when it publishes locations, with the GeoNames credit at the foot of
its pages.

### Positions that cannot be right (the GPS check)

A camera writes the GPS position it has, which is not always where the
picture was taken. A phone in an aeroplane keeps the last fix it had on
the ground, and a camera without a GPS receiver of its own (many 360
cameras and other cameras paired with a phone app) writes the last
position the app gave it, which may be from the day before, at home.
Such a picture would get the wrong place, sit in the wrong spot on the
map and turn up in searches for a place it was never in.

After every scan (and **Build previews** and **Add place names**, also
when one was stopped, and before an export with locations) the GPS check
looks at the positions of the pictures of all libraries together, using
only what is already in the database, and sets aside a position when the
data shows it cannot be right:

- the GPS time saved with the picture shows the fix is older than the
  picture: by more than 15 minutes, or by more than 5 minutes when the
  speed the fix states would have taken the camera over 2 km away in that
  time (a phone standing still keeps a fix of a few minutes that is
  still right). Without a time zone every offset the picture may have
  had must make the fix that old. Only for cameras whose other pictures
  mostly have fresh fixes, so a camera whose clock is a few minutes off
  keeps its positions;
- the camera gave the same position, with the same speed, to pictures
  taken minutes apart (a fix that says it was moving cannot stay put;
  the first of those pictures, taken before the camera could have moved
  away, keep theirs), or marked its fix as not valid;
- the pictures taken nearest in time disagree: most of the 4 nearest
  (at least 2), within 12 hours, are too far away to reach at 1,300 km/h
  (an airliner with the jet stream behind it) in the time between them.
  Copies of one shot count once, pictures at exactly the same position
  (the same cached fix) do not count, and without a time zone the times
  allow for every offset the library used within 36 hours (failing that,
  any library's). A time zone that cannot be right for the position (a
  phone that kept the one it had before a flight) widens the times, and
  so does a date taken from the name of an app's export (named when it
  was exported). A camera without a GPS receiver of its own that saves
  no GPS time and no accuracy and gave one position to pictures taken
  hours apart, with no other position in between (its last known
  position), is weaker still: it is never a reference for other pictures
  and is set aside when more than half of the pictures compared
  contradict it, even if that is only one.

A position set aside gives the picture no place: it is not on the map,
not found by place or `has:gps` searches, and never exported (neither
the position nor its place). Nothing is deleted: the file's own GPS data
stays in the database, and the check decides again after every scan, so
a position comes back when the pictures around it change, and a file
that is changed is judged by the position it gives now. The log of a
scan says how many positions were set aside, and the Manage page's place
names count them.

On this computer the viewer says why ("Location ignored: a picture taken
3 minutes later is 2,100 km away.") and which place the position gave,
with **Use this location anyway**; **Ignore location** among the
picture's actions sets aside a position the check keeps. Your choice
is made for every copy of the shot (an app's export of it, the same file
in another library), follows the picture (by its content, also when it
is moved or renamed) and survives every scan; choosing what the check
decides anyway removes it. `is:gps-ignored` lists the pictures whose
position is set aside. Other computers are not shown the position or
the place (but the original file, which they can download, still holds
its GPS data), and exports never hold them. The settings are
`gps_check` in config.json (see SETTINGS.md): it can be turned off. A
picture whose position was set aside may get an estimated place instead
(see [Estimated places](#estimated-places)).

### Estimated places

Many pictures have no GPS position of their own: cameras without a GPS
receiver, phones that saved a picture without its location, positions the
GPS check set aside. After every GPS check (so after every scan, and at
once when you decide about a place) such a picture gets an **estimated
place** when the index tells where it most likely was:

- **a copy of a located picture**: the same file, a picture taken in the
  same second by a camera of the same model, one with the same dHash taken
  within 2 minutes, or one that looks the same (with looks, see [Looks
  like this](#looks-like-this-and-search-by-a-picture)) taken within 10
  minutes -- not when the camera may have gone more than 2 km in the time
  between them (by the speed its GPS states, or that of its shots just
  before and after: shots of the clouds from an aeroplane look alike);
- **the pictures taken around the same time**: located pictures (any
  library's, unless the GPS check's `across_libraries` is off) taken
  within 30 minutes of it, by the GPS check's clocks (time zones and all;
  a picture whose time zone is uncertain needs the whole range to fit).
  A position whose own accuracy is worse than 2 km (a phone's coarse fix
  on an aeroplane) places nothing. With one before it and one after it
  that are at most 2 km apart, the place between them; with pictures on
  one side only, the nearest -- when it was taken within 15 minutes and
  it and the next shots on that side (at least two) are within 2 km of
  each other. Two sides further apart (a drive, a flight) give no place.
  How far off it may be counts how far the camera may have gone since the
  nearest shot: at the speed of those shots (or their GPS's), at least
  walking pace, and after the first two minutes at least 50 km/h (it may
  have boarded a car, a boat or a helicopter). When that is more than 2
  km, the picture gets the nearest shot's town without a position (no
  pin; the region only beyond 10 km, nothing beyond 25 km);
- **the library's area** (region only, never a pin): when at least 95% of
  a library's located pictures (and at least 20) are within 100 km of each
  other and name one region (or one country), a picture of that library
  that got no place otherwise gets "Tuscany, Italy (estimated from the
  library)" -- when it was taken within 3 hours of those pictures (any
  time zone its clock may have had) and no located picture taken nearer
  in time is somewhere else (not at the airport on the way there, nor on
  the plane), or when it has no date to go by (no date, one an app gave
  it when it exported it) and the located pictures are at least a tenth
  of the library's: a few located pictures in a large library say little
  about the rest.

Only capture dates the GPS check trusts count (the camera's, a video's
own, a date in a file name; not the date a program saved a file: one in
the name an app gave its export, or EXIF DateTime alone with a Software
tag, as an editor writes it). A date from the file's time counts only
when the files around it in its folder kept theirs: at least two pictures
of the folder with a camera date whose files were written within an hour
of it all have file times within 2 minutes of their camera dates (a
camcorder's clips copied from its card together with its photos, say).
Pictures downloaded or exported by an app get the time of the copy, so
they do not borrow places that way. The estimates take a camera's clock
to be right: a camera without GPS whose clock is an hour off (one that
ignores daylight saving) gets the places where the others were an hour
earlier or later.

Tried on about 1,800 located pictures whose positions were hidden (a tenth
at a time, three times over) and estimated again: 61% got an estimate
(two in five of those from a copy, such as a photo's JPEG beside its
HEIC); of those with a position, 99.6% in the right town, none more than
1.5 km off and 99.5% within the distance they say they may be off; those
without one (2 to 25 km off by then) all named the right town. An
estimate always says what it is.

An estimated place is kept apart from the picture's own data: `has:gps`
still means a GPS position in the file, and the file is never changed.
(A town or region without a position is searched and shown, but has no
pin.)
It is searched like any place (`place:`, the Place filter) and shown as
"Lucca, Tuscany, Italy" followed by where it comes from ("estimated from
pictures taken around the same time", or "estimated from a video's GPS
track" when those are the points of a dashcam's track): tap or point at
that for the details ("... one taken 4 minutes earlier, one 2 minutes
later, 310 m apart. It may be off by about 400 m."). The map shows
estimated positions as hollow pins while **Show estimated places** is on
(on by default on this computer; other computers and exports turn it on).
`is:place-estimated` lists the pictures with an estimated place, and the
Manage page's place names count them.

On this computer the viewer offers **Accept** (the estimate becomes the
picture's own place: no longer marked as estimated, a pin like any
other), **Change place...** (type the start of a town's name -- "Lucca",
"Paris, Texas" -- and pick it, or type a position as `lat, lon`) and
**Reject** (the picture has no place); **Set place...** among the
picture's actions gives a place to a picture that has none. Your choice
follows the picture (by its content, also when it is moved or renamed)
and survives every scan; **Clear** or **Estimate again** lets the
estimates decide again. A place you set is never a GPS position
(`has:gps` stays false) and is not used to estimate other pictures. The
settings are `place_estimates` in config.json (see SETTINGS.md); an
export publishes estimated places only when it publishes locations and
**Estimated places** is ticked, places you set with locations -- one you
accepted from an estimate that came from a dashcam's GPS track only as
the track's positions go (with **Drive routes**, and not near where a
drive starts or ends).

### Where 360 pictures open

A 360-degree photo or video opens in the viewer (and shows in its
thumbnail) facing its opening view:

- **Open here** (in the viewer's bar and on the details page, on this
  computer): the picture opens at the view shown, with its zoom, from now
  on, and so do its copies; the thumbnails show that view too.
  **Automatic opening view** (among the picture's actions) forgets it again.
- Otherwise, with [looks](#looks-like-this-and-search-by-a-picture) set up,
  **Update looks** also works out where each described 360 photo opens:
  the image model compares the photo's title and the first sentence of its
  description with views of it every 45 degrees round the horizon, and the
  photo opens at the view that matches its words best -- when that view is
  clearly better than the front (judged by eye on 85 360 photos, three in
  four opened at a good view this way, against half at the front; about
  one photo in three faced its subject better, one in twenty worse). It is
  worked out again when the words, the stitch of the photo or the model
  change, and needs a model that matches words to pictures (such as
  EmbeddingGemma 2; a check with the probe pictures and words for them
  says so first). `"opening_views": false` in
  [image_embedding](SETTINGS.md#image_embedding) turns this off (the next
  run forgets the views it made).
- Everything else opens at the front: the way the camera's front lens
  looked (videos too: their poster is only one moment of them).

The views belong to the picture's content (a moved or copied file keeps
its view) and are kept in the database; published sites open their 360
pictures at the same views (they show no more than the picture itself).

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
library that is turned on, see file and folder names, dates, GPS positions,
place names and the map, camera EXIF data and the model that described each
picture, download the original files with all their metadata, and turn off
"Hide sensitive pictures". Hidden pictures and turned-off libraries stay
out. With search by meaning set up, their searches are embedded by this
computer's embedding model too (and may start it; each computer can ask
a few times a second; only this server's own pages can ask, and other
computers are not told why it is unavailable when it is). In this mode
Manage, Settings and jobs are off, on this computer too;
to manage the libraries at the same time, run a second server without
`--host` on another port (`python serve.py serve --port 5001`). To share
pictures beyond your household, publish a library instead (see [Publishing
a library](#publishing-a-library)): it leaves out what you choose.

## Managing libraries in the browser

The header of every page links to **Duplicates**, **Manage** and
**Settings** when the server runs on this computer (the default address,
`127.0.0.1`), and to **Albums** (see [Favorites, ratings and
albums](#favorites-ratings-and-albums)). Most of what the command line
does can be done there:

- **Manage** lists each library with its folder, whether the folder can be
  reached, how many pictures are described, waiting, out of date or failed,
  and the last job. Its buttons scan a library, describe it, do both, retry
  failed pictures, re-describe pictures whose description is out of date (an
  older prompt, or a video description that talks about its frames),
  refine documents and screenshots (when the library names a refine
  endpoint), update search by meaning (**Update meaning search**, when it
  is set up: the card says how many pictures have a meaning vector, and
  a note at the top says how to set it up when it is not), make the
  pictures' looks (**Update looks**, or **Rebuild looks** after their
  settings changed; see [Looks like this](#looks-like-this-and-search-by-a-picture)),
  and build missing previews. **Process all libraries** does a scan and describe
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
  buttons off. With a `local_server` section in `config.json`, the
  **Model server** panel starts and stops the local model server and
  shows its state, settings and log (see [On your own computer
  (llama.cpp)](#on-your-own-computer-llamacpp)); an embeddings server for
  [search by meaning](#search-by-meaning) is marked "Embeddings", and so
  is its endpoint in the list.
- **Duplicates** goes through the stacks that **Group similar** makes on
  the search page (identical files, and nearly the same shot taken at about
  the same time), the largest first, 20 to a page, for the library picked
  in the header: all of them, only identical files or only similar
  pictures, and only stacks of 3, 5 or 10 or more if you like. Each stack
  shows its pictures side by side with their size, file size and format,
  date (and where it comes from), sharpness and looks, camera and file
  name, and marks the **Best** one, saying why. The best is the full-size
  picture rather than a smaller copy of it (less than half its pixels, the
  same shape and nearly the same look; a smaller picture of another shape
  or with other content is not taken for a copy); then the sharpest (when
  all of them are described; identical files, and one picture saved in two
  formats such as `IMG_0001.HEIC` and `IMG_0001.jpg`, count as one
  picture); then the one that looks least like a copy (converted from the
  camera's format, a file name such as `photo (1).jpg`, `photo - Copy.jpg`
  or `photo_edited.jpg`, a picture saved from a chat app, a screenshot, no
  camera data or a date from the file when the others have the camera's);
  then the most pixels, the format cameras save in (HEIC, then JPEG or
  TIFF, then the others), the largest file, the earliest, and the one
  found first. **Keep best, hide the others** hides the rest of a stack,
  **Keep only this** keeps another picture instead, **Hide** hides one
  picture and **Not a duplicate** takes one out of its stack; **Not
  duplicates** stops stacking the pictures of a stack of up to 100
  (identical files always stay together); **Keep the best in every stack
  on this page** keeps the best of each stack shown, after asking. Nothing
  is deleted: hiding is the same as in the viewer, and **Undo** (or **Show
  hidden pictures** on the search page) brings pictures back. A copy you
  kept stays shown when its hidden twins are deleted or it is moved. "Not
  duplicates" is remembered for each pair of pictures, by their content
  (so it lasts through moves and renames, and a file changed in place
  keeps it), in `data/not-duplicates.json`; the search page and exports
  stop stacking them too. On the search page, an opened stack
  (**Review**) and the viewer's note about a folded stack (**Review
  duplicates**) open that stack on the Duplicates page.
- **Same picture, any date** (on the Duplicates page, with
  [looks](#looks-like-this-and-search-by-a-picture)) lists pictures that
  look the same whenever they were taken, which the search page does not
  stack together: a copy saved again years later, an edited or re-sent
  copy, a picture without a date to go by, or a near-identical shot that
  ended up in another stack (see [Duplicates by look](#duplicates-by-look)).
  It compares the pictures of the library shown in the header, or of all
  of them with **All libraries** (then a copy kept in another library is
  found too). The best and the buttons are the same as for the other
  stacks, except that **Not duplicates** here only keeps apart pictures of
  different stacks of the search page: the pictures of one stack stay
  stacked. A picture that is in a stack on the search page links to it
  (**Its stack of 5**), and that stack offers the way back to the list.

Only the command line can let a scan mark most of a library missing
(`scan.py --allow-mass-removal`; a scan from Manage that stops for this
says so in its log), describe at most N pictures (`--limit`), describe with
another endpoint for one run (`--endpoint`), list every changed file
(`--verbose`), and set the helper module (`settings.py`, or `helper` in
`config.json`). The model server's program, models folder and command line
are only set in `config.json` (`local_server`).

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

1. **Options**, in plain words: the whole library or only the pictures of
   one album (the way to choose what to publish: put them in an album
   first); web-size pictures, originals (with their
   hidden details removed) or thumbnails only; what to leave out
   (screenshots, documents, scans, videos, pictures flagged as private or
   sensitive, pictures not checked yet); whether to also publish file
   names, folder names or locations (and, with locations, estimated
   places and drive routes), and whether "More like this" goes
   by meaning and "Looks like this" goes along (both on by default); and
   the site's title, a footer line, the
   output folder, a zip file and search-engine indexing. The line at the
   bottom counts the pictures that would be published.
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
  place-guess fields, and the place names: then the pages credit GeoNames
  at their foot, the map in a corner; `--estimated-places` adds the
  estimated places, see [Estimated places](#estimated-places); `--routes`
  the routes of drives and the positions their clips have from their GPS
  tracks, see below). Without it,
  GPS positions written
  out in the text in a picture or in its title or description (a
  dashcam prints its position on its video, `N:12.34567 E:65.43210`, and
  the model reads it) are taken out of them too; the frames themselves
  still show what the camera printed. Hidden pictures are never published.
- The positions dashcam clips have from their GPS tracks, and the routes
  of drives (see [Routes](#routes-a-dashcams-gps)), go out only with
  `--location` and `--routes` (**Location** and **Drive routes**): then
  `data/routes.js` holds the tracks of the published clips (rounded as
  positions are), which the map draws and the viewer follows. Every point
  within 500 m of where a drive starts or ends is left out (a route that
  passes that near is cut there), so that a home is not shown; `--routes-trim
  M` (or `"routes_trim"` in the library's saved export options, 0 to 5,000)
  changes the 500 m (where the drives of every library start and end). A
  clip whose position is left out keeps its place names, and so does one
  without the option; a place estimated from a track (with **Estimated
  places**), or one you accepted from such an estimate, goes the same way.
  The positions a dashcam clip's title, description and text (and its
  drive's) write out, as its overlay prints them, are never published --
  with or without the routes: a clip without a fix prints the last
  position it had, often where the drive started. The review says how
  many routes go, how many clips keep only their place names, and how
  many clips to publish are a dashcam's: their pictures show what the
  overlay prints, whatever the options, so it warns about those filmed
  near where a drive starts or ends (or without a fix) -- hide them to
  leave them out.
- `--album NAME` (an album's name or id; **Which pictures** in the
  dialog) publishes only the pictures of that manual album that are in
  the library, still left out by the rules above when they say so; the
  review counts only those. A smart album cannot be used: **Copy to a
  new album** on the Albums page makes a manual one of its pictures.
- Your favorites, ratings and album names are published only with
  `--marks` (**Favorites, ratings and albums** in the dialog, `marks` in
  the saved options): then the site's `is:favorite`, `rating:` and
  `album:` work, and it lists the albums that hold published pictures
  (in their order) and your smart albums with their searches as you saved
  them. A smart album is left out when its search cannot be published: one
  limited to another library, one that searches by folder, file name or
  place (`folder:`, `file:`, `place:`, `has:gps`, the Place filter) while
  those are not published, by library (`lib:`), or for pictures like one
  that is not published. Without `--marks`, not even the name of the
  album the export is limited to goes out.
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
- Videos are published as their poster frame. With **Include videos** in
  the dialog (`--videos include`, or `"videos": "include"` in the saved
  options) the videos browsers can play (H.264, or a playable copy made
  with `video_proxies`) are published too, up to `max_video_mb` (200 MB)
  each: copied into an MP4 without their metadata (no place, date or
  device; the picture and sound are not re-encoded). HEVC videos without
  a playable copy keep their poster only; the review step says how many.
  A 360-degree video goes out as its LRV (or its playable copy), both
  lenses side by side, which the site's viewer stitches as it plays.
  The AI model checked only a few still frames of each video for private
  or sensitive content and never heard its sound: watch the videos you
  publish. **Videos** under **Leave out** leaves every video out.
- With `similar` (on by default) the meaning vectors of the published
  pictures go along (about 350 bytes each), so **More like this** and
  `similar:` go by meaning in the site. Only vectors made from a
  picture's current description are published, and a vector holds
  nothing its description does not show; the review says how many
  pictures have one (update search by meaning first, see [Search by
  meaning](#search-by-meaning)). The pages load the vectors only when a
  search first needs them.
- With `looks` (on by default) the look vectors of the published pictures
  go along (about 350 bytes each), so **Looks like this** and `look:` work
  in the site; the review says how many pictures have one (see [Looks like
  this](#looks-like-this-and-search-by-a-picture)). A look vector is made
  from the picture itself, which the site shows anyway. Searching by a
  dropped picture needs the image model of your server: published sites
  do not have it.
- With **Search by meaning in the browser** (`meaning`, on by default when
  it can be used) the embedding model goes along too, so typed searches
  in the site find pictures by meaning as they do on your server, with the
  same **Words / Words + meaning / Meaning** switch. It needs a small
  model that can run in a browser (mdbr-leaf-ir, see [Search by
  meaning](#search-by-meaning); not Qwen3-Embedding), its license file and
  up-to-date vectors; the Export dialog says what is missing, and the
  review step how large it is: about 31 MB of files, of which a visitor
  downloads about 23 MB (web hosts send them compressed) once, at their
  first search by meaning, never before. Visitors who choose **Words**
  never download it, and with their browser's data saver on a **Search by
  meaning (23 MB)** button asks first. Until it has loaded the site shows
  the word matches, with the download's progress above them; the
  pictures found by meaning join them a moment later. It then works
  offline and when the site is opened from disk. It runs in the page
  (WebAssembly when the browser allows it, plain JavaScript otherwise,
  with the same results, about five times slower) and checks itself when
  it loads: a damaged download, a browser that cannot run it, or one that
  makes other vectors than your server did, searches by the words, with a
  note above the results. Nothing is asked of any other site. A web host
  that sends a `Content-Security-Policy` header must allow
  `'wasm-unsafe-eval'` in its `script-src` for the WebAssembly part
  (GitHub Pages sends none); without it the model runs in plain
  JavaScript. The search and details pages credit the model at their foot
  and link to its license (`data/meaning/LICENSE.txt`; the map page,
  which does not use the model, has no foot); the model files hold only
  the model (its weights and vocabulary), two made-up test sentences and
  their vectors.
- `--zip` also writes `<folder>.zip` next to the folder. A file of that
  name is only replaced when an earlier export made it and it was not
  changed since; otherwise the export stops before writing anything. The
  pages ask search engines not to list them or their pictures (and a
  `robots.txt` says the same) unless `allow_indexing` is set, but anyone
  who has the address can open the site.

Try the site on this computer with export.py's own small web server (the
export prints the command for its folder), for example:

    python export.py --serve data/exports/family

and open http://localhost:8000/ (`--port` takes another port; only this
computer can open it; `Ctrl+C` stops it). Any static web server works
too, but `python -m http.server` cannot move in a video (it does not
answer the range requests browsers make for that): drives and videos only
play from their start there. To publish it, copy everything in the folder
to any host that serves plain files (GitHub Pages, Netlify, your own web
server); nothing needs to be installed there. Export again after
describing more pictures: only what changed is written.

Opened from disk, the 360-degree view shows the flat panorama (browsers do
not allow it for files), and a 360-degree video plays flat (its front
lens); over http they can be turned around.

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
  not sent to a cloud endpoint unless you name them with `--library`, and
  hidden pictures are never sent to one (they are listed as not run). An
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
including those only a text editor changes (`reader_threads`,
`purge_after_days`, `nadir_fill_deg`, `zenith_fill_deg` and
`level_horizon`).

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
  GPS data, the descriptions, your own titles, descriptions and tags
  (kept apart from the model's), your favorites, ratings and albums, the
  positions the GPS check set aside (with your choices), the estimated
  places, the GPS tracks read from videos (where a dashcam drove: see
  [Routes](#routes-a-dashcams-gps)) and where 360 pictures open.
- `thumbs.db` and `previews/` - thumbnails, and 2048 px previews, 360
  panoramas and the contact sheets of videos (about 0.5 MB per picture).
- `proxies/` - playable copies of videos (only with `video_proxies`).
- `drives.json` - the descriptions of drives (runs of video clips, see
  [Drives](#drives)); `drive-previews/` - their sped-up previews (only
  with `drive_previews`).
- `jobs.db` and `logs/` - the Manage page's jobs and their logs, which name
  files. With the `LOG_ERRORS` environment variable set, the LLM's replies
  to failed requests are saved in `logs/` too. `logs/model-server.log` is
  the output of the model server the Settings page starts, and
  `model-server.json` (changed under `model-server.lock`) records it (its
  process id and start time) while it runs.
- `vectors.db` - the meaning vectors of the pictures (see [Search by
  meaning](#search-by-meaning)); made again when deleted.
- `image_vectors.db` - the look vectors of the pictures (see [Looks like
  this](#looks-like-this-and-search-by-a-picture)); safe to delete, but
  making them again takes a long run of the image model.
- `exports/` - static sites (`exports/<library name>/`) and the list of
  files each export wrote (`exports/state/`).
- `reports/` - model comparison reports.
- `not-duplicates.json` - the pictures you said are not duplicates (the
  Duplicates page).
- `cache/` (near-duplicate groups, rebuilt when missing) and `tmp/`.
- `places/` - the GeoNames files and the place index made from them (see
  [Place names](#place-names)); not private, and made again by
  `make_places.py`.
- `tiles/` and `blue_marble.jpg` (from `make_tiles.py`), `world_map.png`
  (from `world_map.py`).
- `images.db` and `thumbs/` - the database of older versions (see
  [Upgrading](#upgrading-from-an-older-version)).
- `*.lock` files and `abort` - used while tools run.

To keep the descriptions, back up `config.json`, `data/index.db`,
`data/drives.json` and `data/not-duplicates.json` while no tool is running.

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

    python make_places.py

downloads the place names and looks up the places of the pictures (see
[Place names](#place-names)).

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
- `sheet_talk.py` - spots video and drive descriptions that describe the
  frames of the contact sheet instead of the video (they are asked again)
- `refine.py` - which pictures the refine pass describes again
- `local_server.py` - starts and stops the local model servers (Settings
  page, describe and embed jobs, searches by meaning, the daily stop time)
- `browser_model.py` - the embedding model of search by meaning made
  ready for a published site (and the WebAssembly kernel of
  `static/embed-engine.js`, which runs it there)
- `embeddings.py` - search by meaning: the meaning vectors of the
  pictures (`data/vectors.db`), the embed step, and the server's answers
  to `/api/vectors` and `/api/embed`
- `looks.py` - looks: the look vectors of the pictures
  (`data/image_vectors.db`), the job that makes them, and the server's
  answers to `/api/looks` and `/api/looks/query`
- `export.py` - publish one library as a static website
- `compare_models.py` - compare how endpoints describe the same images
- `progress.py`, `world_map.py`, `make_tiles.py`, `make_places.py` - see
  [Other tools](#other-tools)
- `videos.py` - videos: what ffprobe says, posters, contact sheets,
  copies without metadata and playable copies (ffmpeg); 360-degree videos
  (their LRV, stitched frames, the lens geometry the viewer uses)
- `insta360.py` - the data Insta360 cameras write at the end of their
  files, and the level tracks of 360 videos and photos made from their
  motion sensor (see [Level horizons](#level-horizons))
- `opening_views.py` - where 360-degree pictures open (see [Where 360
  pictures open](#where-360-pictures-open))
- `drives.py` - drives: runs of video clips recorded one after the other,
  their descriptions and their sped-up previews (see [Drives](#drives))
- `tracks.py` - the GPS tracks of videos (a dashcam's) and the routes of
  drives (see [Routes](#routes-a-dashcams-gps))
- `places.py` - place names: the GeoNames download, the place index and
  the nearest town of a GPS position (`static/place-names.js` is their
  part of the Manage page)
- `gps_check.py` - the GPS check: positions that cannot be where a
  picture was taken are set aside (see [the GPS
  check](#positions-that-cannot-be-right-the-gps-check))
- `place_estimates.py` - estimated places for pictures without a usable
  position, and your own places (see [Estimated
  places](#estimated-places))
- `example_helper.py` - the hooks a helper module can have
- `search_index.py` - the compact search records the search page downloads
  and searches in the browser, and the groups of near-duplicate pictures
- `image_edits.py` - your own titles, descriptions and tags, kept apart
  from the model's descriptions and laid over them
- `albums.py` - your favorites, ratings and albums (and how they follow
  moved files)
- `duplicates.py`, `not_duplicates.py` - the Duplicates page's stacks and
  best pictures, and the pictures kept apart as not duplicates
- `admin.py`, `library_ops.py` - the Manage and Settings pages' server side
  (library changes, folder checks, jobs, endpoints)
- `static/`, `templates/` - the web pages (`shell.js` is the header shared
  by every page; `site.js` is where the pages get their data, from the
  server or from a static export; `search-core.js` is the search engine and
  `viewer.js` the picture viewer, both shared with a static export;
  `routes.js` draws drive routes, for the map and the viewer;
  `embed-engine.js` and `embed-kernel.js` run the embedding model in a
  published site (only there, and only once a search needs it);
  `app.js`, `detail.js` and `map.js` run the search, detail and map pages;
  `duplicates.js` the Duplicates page, `albums.js` the Albums page;
  `manage.js` and `settings.js` run
  Manage and Settings, `model-server.js` the Settings page's Model server
  panel, and `export-review.js` holds the rules and words of the Export
  dialog)
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
- When a newer version changes the database's layout (to keep your own
  titles, descriptions and tags, say), the first run of any tool makes the
  change and saves a copy of the database as it was first
  (`data/index.db.bak-v<old version>`); delete the copy once all is well.
  An older checkout cannot open the changed database.
- Videos are indexed now (with ffmpeg installed; see [Videos](#videos)):
  the first scan after upgrading reads the videos in your libraries. The
  database gets two columns for them (`media`, `duration`), with the usual
  copy of the database as it was (`data/index.db.bak-v4`).
- Favorites, ratings and albums are kept in three new tables of the
  database, made by the first run after upgrading (with the usual copy of
  the database as it was, `data/index.db.bak-v5`); nothing else changes.
- The prompt is now version 6: only what a video is told changed (its
  title and description are about what happens in it, never about the
  frames the model sees). Pictures described by version 5 are up to date;
  videos are out of date, so **Re-describe out of date** on Manage (or
  `scan.py --redescribe-stale`) describes the videos again, and only
  them. Until then they keep their descriptions.
- Video descriptions of version 6 (and drive descriptions) that still
  talk about the frames ("four frames show ...") now count as out of date
  too, and **Re-describe out of date** describes those again, asking twice
  when needed (see [Videos](#videos)). `"video_sheet_check": false` leaves
  them as they are.
- Videos from dashcams and other cameras that write their own clock's
  time as if it were UTC were dated hours off (see [Videos](#videos)); the
  first scan after upgrading reads the dates of those videos again and
  gives them their names' times.
- Insta360 360-degree videos (`.insv`) are indexed now too (see
  [360-degree videos](#360-degree-videos)): the first scan after upgrading
  reads them; their LRV copies and second lens files are never indexed.
  Insta360 videos read again (changed files) also get the camera's local
  time and time zone from their names.
- Search by meaning now recommends a small model, mdbr-leaf-ir, which also
  runs in published sites, and its `query_prefix` is the new default.
  With Qwen3-Embedding set `query_prefix` to its instruction (see
  [SETTINGS.md](SETTINGS.md#embedding)), as the default changed; a model
  of its size cannot go with a published site. Switching models makes
  every meaning vector again (**Update meaning search**: a few minutes
  with the small model on a CPU), and so does the first embed run after
  upgrading when the model file is known (`model_file`, or a `local_server`
  entry): the vectors are tied to that file now.
- Unstitched 360-degree photos (Insta360 `.insp`) are stitched again by the
  first scan after upgrading, now with the seams blended: only their
  previews, panoramas and thumbnails change. The soft fill over a tripod or
  a hand at the bottom is off unless you set `nadir_fill_deg`.
- 360-degree videos and ONE R photos are levelled now (see [Level
  horizons](#level-horizons)): the first scan after upgrading reads the
  360 videos and unstitched 360 photos again for their level tracks and
  makes their posters, contact sheets, previews and thumbnails anew
  (descriptions are kept; describe them again if you want descriptions of
  the level pictures). Their looks are made again by the next **Update
  looks**.
- 360-degree pictures can open facing their subject now (see [Where 360
  pictures open](#where-360-pictures-open)): with looks set up, the next
  **Update looks** works it out for the described 360 photos (a few
  seconds a photo on a CPU) and makes their thumbnails again (the Manage
  page offers it, with the number of photos waiting for a view). The views are
  kept in a table of their own in `index.db` (the database's version does
  not change).
- Dashcam videos' GPS tracks are read now (see [Routes (a dashcam's
  GPS)](#routes-a-dashcams-gps)): the first scan after upgrading reads the
  track of every video indexed before -- a few small reads a video, no
  ffmpeg (a fraction of a second a video on a network share).
  Clips with a track get a position, place names and their clock's UTC
  offset, and drives their routes. The tracks are kept in a table of their
  own in `index.db` (the database's version does not change).
- Pictures without a position get an estimated place now (see [Estimated
  places](#estimated-places)): the GPS check of every scan works them out,
  the first one for all of them (the search index then sees those pictures
  as changed). They are kept in tables of their own; a picture's own
  position and place do not change. An export publishes neither estimated
  places nor routes unless its new options say so.
- A GPS position written in a picture's text (a dashcam's overlay, read by
  the model) is now left out of its meaning vector, so the next **Update
  meaning search** (or scan) makes the vectors of those pictures again.
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
is installed, and are skipped when it is not. No test needs a model or
ffmpeg: `tests/fake_llm.py`, `tests/fake_model_server.py` and
`tests/fake_embed_server.py` stand in for the models on 127.0.0.1, and
`tests/fake_ffmpeg.py` for ffmpeg and ffprobe. A few video tests also use
a real ffmpeg when there is one on the PATH (or named by the
`SEARCH_IMAGES_TEST_FFMPEG` environment variable), and are skipped
otherwise. The browser's embedding engine is tested on tiny made-up
models (`tests/fake_gguf.py`) against what llama.cpp makes of them,
kept in `tests/fixtures/fake_bert_llamacpp.json`; after a change to the
made-up models, `SEARCH_IMAGES_LLAMA_SERVER=/path/to/llama-server python
tests/make_embed_fixture.py` makes that file again.

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

Place names (downloaded when you ask for them, see [Place
names](#place-names)) are from GeoNames, https://www.geonames.org/, under
the Creative Commons Attribution 4.0 licence
(https://creativecommons.org/licenses/by/4.0/).
