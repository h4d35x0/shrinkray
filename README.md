# shrinkray

Bulk-shrink a folder of video into a phone-sized, properly named, browsable
library.

Point it at a directory of video and it produces a second copy at a fraction
of the size, named properly, foldered, and browsable.

How big a fraction depends on the content, not on the input size. Measured with
the same settings: conference talks came out at **18%** of source, screen
recordings at **6%**, and video that was already compressed at **97%**, which
is to say no gain at all. shrinkray estimates this by measuring your actual
source bitrate, and tells you plainly when there is nothing to win.

It was built for conference recording packages, which ship a folder of files
called things like `XX34-105.mp4` alongside an HTML index that knows what those
files actually are. It reunites the two. But the index is optional: `--scan`
works on any folder of video at all.

The encoding, verification, resume and supervision machinery is the bulk of it
and cares nothing about what the video contains.

Measured on one 306-talk package: **105 GB and 173 hours in, 20 GB out**, with
every file named for its talk and foldered by track. About 16 hours of
unattended encoding on a consumer GPU.

## What it does

| Script | Job |
|---|---|
| `build_index.py` | Build `library.json` from an HTML index, or from a plain folder with `--scan` |
| `transcode.py` | Encode to 720p HEVC. Resumable, atomic writes, verified output |
| `make_index.py` | Write a searchable `index.html` for the result |
| `extract_audio.py` | Stream-copy the audio into `.m4a` for listening without video |
| `supervise.ps1` | Restart the transcode whenever it stops early (Windows) |
| `shrinkray_gui.py` | A window, for people who do not want a command line |

## The window

If a command line is not your thing:

```
python shrinkray_gui.py
```

Pick a folder, pick what kind of video it is, press Start. Three presets carry
the settings so nobody has to know what CQ means:

| Preset | For | Audio |
|---|---|---|
| Talk or lecture | a person speaking, slides | 64 kbps mono |
| Music or performance | recorded sets, anything musical | 160 kbps stereo |
| Camera footage | general video | 128 kbps stereo |

It samples your files to estimate the result before you commit, shows a
progress bar while it runs, and can be cancelled. Cancelling is safe: finished files are kept and a later
run resumes from them. It drives the same scripts as the command line, so
there is one encoder path and one verification path, not two.

Tkinter ships with Python, so the window adds no dependencies. ffmpeg is still
required, and the app checks for it at startup rather than failing later.

## Requirements

- Python 3.10+
- `ffmpeg` and `ffprobe` on `PATH`
- For hardware encoding, an NVIDIA GPU with NVENC. CPU encoding via `libx265`
  works everywhere, roughly 3x slower.

## Usage

```
python build_index.py --root /path/to/videos     # once, about a minute
python transcode.py                              # the long part, resumable
python make_index.py                             # browsable index
python extract_audio.py                          # optional, audio-only copies
```

### Two ways to build the library

**Indexed** (default). For recording packages that ship a "Start Here" HTML
page mapping opaque filenames to real titles and speakers. Any `*.html` one
directory below the root that links to `movies/...` is used, so this is not
tied to a particular conference, year, or filename.

**Scan** (`--scan`). No index required. Every video under the root becomes a
record, titled from its filename and grouped by its parent directory. This is
the mode for any other folder of video: recorded sets, lecture captures,
camera footage.

```
python build_index.py --scan --root /path/to/footage
```

### Music, not speech

The defaults are tuned for a person talking: 64 kbps **mono** audio is
transparent for speech and halves the audio budget. Music needs stereo and far
more bitrate, and motion-heavy footage needs a lower `--cq`:

```
python transcode.py --audio-kbps 160 --audio-channels 2 --cq 30
```

`transcode.py` resumes by checking which outputs already exist and verify, so
**re-running the identical command is the retry mechanism.** Interrupt it
freely; you lose at most the files in flight.

On Windows, `start-supervisor.cmd` runs the whole thing unattended and restarts
it if it dies.

### Options worth knowing

```
--encoder nvenc|x265     default nvenc
--cq 34                  NVENC cq or x265 crf. Lower is bigger and better
--height 720             target height; never upscales
--workers 2              concurrent encoders
--audio-kbps 64          audio bitrate
--audio-channels 1       1 for speech, 2 for music
--collection "Name"      written into each file's album tag
--limit 3                smoke test on the first N videos
--dry-run                print the plan, touch nothing
--verify                 thoroughly check existing output, change nothing
```

`make_index.py` takes `--title` for the generated page's heading.

## Notes from building it

**Two workers, not one.** One 720p HEVC stream does not saturate a single NVENC
block. On a Turing card, one worker measured 7.7x realtime with the encoder
about 45% busy; two measured 10.9x with it pinned at 100%. The second worker is
worth about 40%. More than two buys nothing, since the block is then saturated.

**`+faststart` makes an mp4 lie about its length.** It relocates the moov atom
to the front of the file, so a file truncated to half its bytes still reports
its full original duration. Any integrity check that compares container
duration alone will pass a corrupt file.

**Verify by decoding, and read stderr rather than the exit code.** `ffmpeg -f
null` returns 0 on a truncated file while printing `partial file` and
`Invalid NAL unit size` to stderr. Checking only the exit code passes corrupt
files silently. Seek-based checks are worse still: anything that seeks relative
to the container's claimed duration seeks past the real data on a truncated
file, decodes nothing, exits clean, and reports success.

**Distinguish "bad" from "could not tell".** A probe that times out or fails
says nothing about the file. Collapsing that into "bad" means a busy machine
deletes finished work. Verification here returns one of three verdicts, and
only a definite failure may delete anything. Retrying an ambiguous check does
not help either: if a check can be wrong for a reason that persists across
attempts, repeating it returns the same wrong answer and makes it look
confirmed.

**Output size follows the target quality, not the input size.** A fixed
"expect 20% of the original" ratio is wrong in both directions: the same
settings produced 18% on talks, 6% on screen recordings, and 97% on already
compressed video. The estimate here predicts an output *bitrate* from the
quality setting and caps it at the measured source bitrate, so pointing the
tool at its own output reports "already compressed" instead of promising a
saving that cannot happen.

**Cheap checks inline, expensive ones alone.** Container and video-stream
durations are instant and reliable, so they run after every encode. The full
decode is reserved for `--verify`, which runs single-threaded with nothing else
touching the disk, reports what it finds, and deletes nothing.

## Filenames

Titles come from the package's own index, then get made safe for Windows, exFAT
and Android: a colon becomes ` -`, a slash becomes `-`, curly quotes and dashes
are flattened to ASCII, reserved device names are prefixed, and over-long names
are truncated. Hyphens *inside* words are preserved, so `DHCP-Assisted` does not
turn into `DHCP - Assisted`. Output is checked for collisions before any
encoding starts.

## Licence and content

MIT, for the code in this repository.

The toolkit does not include, download or distribute any recordings. It
operates on a package you already have. Whatever licence applies to your
recordings still applies after transcoding, and many conferences publish their
talks freely through their own channels, which is the better place to point
someone who wants a copy.
