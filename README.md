# Cutty Sark

Mark clips—by start time, end time, and an optional label—within a single long video file,
appending each one to a CSV as you go.

It's a local web UI served by a small Python script. The browser plays and scrubs the
video; the Python process owns the file on disk (which browsers can't do).

No dependencies, no build step. Just Python 3 and the two files in this repo.

_Please note that 99% of this was created by Claude._ It’s in my repo, but it feels weird
to call this something "I" "made."

![Screenshot of the UI](screenshot.jpg)

## Usage

```
python3 clipper.py /path/to/movie.mp4
```

That starts a server on `127.0.0.1`, prints the URL, and opens your browser. Ctrl-C
stops it. Clips are written to `movie.clips.csv` next to the video.

Options:

| Flag | Meaning |
|---|---|
| `--csv PATH` | Write clips somewhere other than `<name>.clips.csv` |
| `--captions PATH` | Use a specific caption file |
| `--port N` | Bind a specific port (default: a free one) |
| `--no-browser` | Don't open a browser |

If a `.vtt` or `.srt` file sits beside the video (`movie.vtt`, `movie.en.srt`), it's
picked up automatically and can be toggled with `C`. SRT is converted to WebVTT on the
fly.

## Keyboard

The keyboard is the real interface—marking dozens of clips means hands stay put.

| Key | Action |
|---|---|
| `Space` | Play / pause |
| `←` / `→` | Nudge 1s |
| `Shift` + `←` / `→` | Nudge 0.1s |
| `,` / `.` | Nudge 1s |
| `J` / `L` | Nudge 5s |
| `I` | Set in point at current time |
| `O` | Set out point at current time |
| `Z` | Seek to the pending in point, to review before saving |
| `C` | Toggle captions |
| `Enter` | Save clip |
| `Tab` | Jump to the label field |
| `Esc` | Clear the pending in/out |

## Output

```csv
start,end,duration,label
92.400,101.200,8.800,"Opening line"
254.000,258.500,4.500,
```

Seconds as decimals, which sort numerically and drop straight into
`ffmpeg -ss <start> -to <end>`. `duration` is derived, for eyeballing. Rows are in the
order you marked them, not sorted by time—the file is a log of what you did.

Reopening against an existing CSV loads it and appends. The original is copied to
`.csv.bak` on first load.

## Not losing clips

Each clip is a separate request that returns only once the file write has landed. The
server holds the authoritative list and rewrites the whole CSV to a temp file, then
`os.replace()`s it into place—atomic, so an interrupted write can't truncate anything.
Pending in/out points survive in `localStorage`, and a failed save leaves the clip on
screen rather than silently dropping it.

## If the video won't play

Browsers won't decode everything (MKV, ProRes, some AV1). Clipper notices and prints an
`ffmpeg` command for a browser-friendly proxy:

```
ffmpeg -i movie.mkv -c:v libx264 -crf 23 -preset veryfast -c:a aac proxy.mp4
```

Timings marked against the proxy are valid for the original — the transcode preserves
timestamps.

## Files

```
clipper.py     # server: static files, video streaming with Range support, CSV
clipper.html   # the entire UI, inline
```

## License

MIT. See [LICENSE](LICENSE).
