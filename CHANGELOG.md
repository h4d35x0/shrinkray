# Changelog

## 0.1.0

First release.

### Added
- `build_index.py` builds `library.json` from a recording package's own HTML
  index, or from any plain folder of video with `--scan`.
- `transcode.py` encodes to 720p HEVC via NVENC or libx265. Resumable by
  verified output file, atomic `.part` writes, per-file verification, failure
  counting, filename-collision detection before any encoding starts.
- `make_index.py` writes a searchable `index.html` plus a CSV of the result.
- `extract_audio.py` stream-copies the audio track into `.m4a`, for listening
  without video. No re-encode, so no quality loss.
- `supervise.ps1` restarts the transcode whenever it stops early, waits for
  free memory before each attempt, and refuses to loop forever around a real
  defect. Distinguishes an out-of-memory kill from a genuine failure.
- `shrinkray_gui.py`, a Tkinter window with three presets, a size estimate, a
  progress bar and a cancel button. No dependencies beyond the standard
  library. It drives the same scripts as the command line.
- `--verify` performs a full decode of every output, reports what it finds, and
  deletes nothing.

### Notes
- Verification is three-valued. A probe that fails says nothing about the file,
  so "could not measure" never deletes anything; only a definite failure does,
  and every deletion is logged with its reason.
- Cheap checks (container and video-stream duration) run inline after each
  encode. The expensive full decode runs only under `--verify`, alone.
- Filenames are made safe for Windows, exFAT and Android without mangling
  hyphenated words.
- Audio defaults to 64 kbps mono, which suits speech. Music wants
  `--audio-kbps 160 --audio-channels 2`.
