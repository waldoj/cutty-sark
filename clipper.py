#!/usr/bin/env python3
"""Clipper — mark clips within a long video file, appending each to a CSV.

    python3 clipper.py /path/to/movie.mp4

Serves a local web UI on 127.0.0.1, streams the video with Range support, and
owns the CSV on disk. Ctrl-C to stop.
"""

import argparse
import csv
import http.server
import json
import mimetypes
import os
import pathlib
import re
import shlex
import shutil
import socketserver
import subprocess
import sys
import threading
import urllib.parse
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(HERE, "clipper.html")

FIELDS = ["start", "end", "duration", "label"]
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")

# Size of each chunk shovelled from disk to socket. 64K keeps memory flat while
# still being large enough that syscall overhead doesn't matter.
CHUNK = 64 * 1024


# Sidecar caption extensions, most-preferred first. VTT is what browsers
# actually render; SRT is what most tools produce, so it gets converted.
CAPTION_EXTS = (".vtt", ".srt")

# Bounded whitespace around the arrow: unbounded \s* can backtrack badly on a
# long run of blanks that never reaches "-->", and this runs over whole files.
SRT_TIME_RE = re.compile(
    r"(\d{1,3}):(\d{2}):(\d{2})[,.](\d{1,3})[ \t]{0,8}-->[ \t]{0,8}"
    r"(\d{1,3}):(\d{2}):(\d{2})[,.](\d{1,3})"
)


def find_captions(video_path):
    """Look for a caption file beside the video: movie.mp4 → movie.vtt / movie.srt.

    Also accepts a language-tagged name (movie.en.srt), which is what most
    subtitle downloaders produce.
    """
    base, _ = os.path.splitext(video_path)
    directory = os.path.dirname(video_path) or "."
    stem = os.path.basename(base)

    for ext in CAPTION_EXTS:
        exact = base + ext
        if os.path.isfile(exact):
            return exact

    # movie.en.srt, movie.forced.vtt, etc. — first match wins, sorted for
    # determinism so the same file is picked run to run.
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return None
    for ext in CAPTION_EXTS:
        for name in names:
            if name.startswith(stem + ".") and name.lower().endswith(ext):
                return os.path.join(directory, name)
    return None


def srt_to_vtt(text):
    """Convert SRT to WebVTT — the only caption format browsers will render.

    SRT differs in three ways that matter: it needs a WEBVTT header, it uses a
    comma for the decimal separator in timestamps, and its cues are numbered.
    The numbering is harmless in VTT (it becomes a cue identifier), so only the
    header and separator actually need fixing.
    """
    def fix(match):
        h1, m1, s1, ms1, h2, m2, s2, ms2 = match.groups()
        # Well-formed SRT always has 3 millisecond digits. For malformed short
        # fields, pad on the right: the field is a decimal fraction, so ",5"
        # reads as half a second, matching how ".5" reads in the VTT output.
        # VTT also requires a 2-digit hour, which SRT doesn't always supply.
        return (
            f"{int(h1):02d}:{m1}:{s1}.{ms1.ljust(3, '0')}"
            f" --> {int(h2):02d}:{m2}:{s2}.{ms2.ljust(3, '0')}"
        )

    body = SRT_TIME_RE.sub(fix, text.replace("\r\n", "\n").replace("\r", "\n"))
    return "WEBVTT\n\n" + body.lstrip("﻿").lstrip()


def parse_range(header, size):
    """Parse a single-span `Range: bytes=…` header.

    Returns an inclusive (start, end) byte pair, or None if the range is
    unsatisfiable and the caller should reply 416. Multi-range requests aren't
    supported — browsers don't use them for video.
    """
    match = RANGE_RE.fullmatch(header.strip())
    if not match:
        return None
    first, last = match.group(1), match.group(2)

    if first == "":
        # Suffix form: `bytes=-N` means the final N bytes.
        length = int(last or 0)
        if length == 0:
            return None
        start, end = max(0, size - length), size - 1
    else:
        start = int(first)
        end = int(last) if last else size - 1

    end = min(end, size - 1)
    if start > end or start >= size:
        return None
    return start, end


class ClipStore:
    """The authoritative clip list. Rewrites the whole CSV on every mutation.

    Full rewrite rather than append means there is no append-position state that
    can drift out of sync, and the write is atomic via os.replace, so an
    interrupted write can never leave a truncated file.
    """

    def __init__(self, csv_path):
        self.csv_path = csv_path
        self.lock = threading.Lock()
        self.clips = []
        self._load()

    def _load(self):
        if not os.path.exists(self.csv_path):
            return

        # Paranoia: keep a copy of whatever was here before we ever touch it.
        try:
            shutil.copy2(self.csv_path, self.csv_path + ".bak")
        except OSError as exc:
            print(f"warning: could not back up {self.csv_path}: {exc}", file=sys.stderr)

        with open(self.csv_path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                try:
                    start = float(row.get("start", ""))
                    end = float(row.get("end", ""))
                except (TypeError, ValueError):
                    continue  # header junk or a hand-edited row we can't parse
                self.clips.append(
                    {"start": start, "end": end, "label": (row.get("label") or "").strip()}
                )
        print(f"loaded {len(self.clips)} existing clip(s) from {self.csv_path}")

    def _write(self):
        """Write the full list to a temp file, then atomically replace."""
        tmp = self.csv_path + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh, quoting=csv.QUOTE_MINIMAL)
            writer.writerow(FIELDS)
            for clip in self.clips:
                writer.writerow(
                    [
                        f"{clip['start']:.3f}",
                        f"{clip['end']:.3f}",
                        f"{clip['end'] - clip['start']:.3f}",
                        clip["label"],
                    ]
                )
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.csv_path)

    def as_json(self):
        with self.lock:
            return [self._decorate(c) for c in self.clips]

    @staticmethod
    def _decorate(clip):
        # Round at the boundary: raw float subtraction yields 8.799999999999997,
        # and the UI shouldn't have to know that. 3 places matches the CSV.
        return {
            "start": round(clip["start"], 3),
            "end": round(clip["end"], 3),
            "duration": round(clip["end"] - clip["start"], 3),
            "label": clip["label"],
        }

    def add(self, start, end, label):
        with self.lock:
            self.clips.append({"start": start, "end": end, "label": label})
            self._write()
            return len(self.clips) - 1

    def update(self, index, start=None, end=None, label=None):
        with self.lock:
            clip = self.clips[index]
            new = dict(clip)
            if start is not None:
                new["start"] = start
            if end is not None:
                new["end"] = end
            if label is not None:
                new["label"] = label
            if new["end"] <= new["start"]:
                raise ValueError("end must be greater than start")
            self.clips[index] = new
            self._write()

    def delete(self, index):
        with self.lock:
            del self.clips[index]
            self._write()


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------

    def log_message(self, fmt, *args):
        # Range requests are chatty; only surface the interesting ones.
        if self.path.startswith("/video"):
            return
        super().log_message(fmt, *args)

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, message):
        self._send_json({"error": message}, status=status)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def _clip_index(self):
        """Parse /clips/<index>, returning the int or None."""
        match = re.fullmatch(r"/clips/(\d+)", urllib.parse.urlparse(self.path).path)
        return int(match.group(1)) if match else None

    # -- GET --------------------------------------------------------------

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            return self._serve_html()
        if path == "/video":
            return self._serve_video()
        if path == "/clips":
            return self._send_json(self.server.store.as_json())
        if path == "/captions":
            return self._serve_captions()
        if path == "/meta":
            video = self.server.video_path
            csv_path = self.server.store.csv_path
            return self._send_json(
                {
                    "video": os.path.basename(video),
                    "csv": os.path.basename(csv_path),
                    # Absolute file:// URLs so the footer names can open the real
                    # files. Built here rather than in the page, which only ever
                    # sees basenames and couldn't reconstruct the directory.
                    "videoUrl": pathlib.Path(video).as_uri(),
                    "csvUrl": pathlib.Path(csv_path).as_uri(),
                    "dirUrl": pathlib.Path(os.path.dirname(csv_path)).as_uri(),
                    # False until the first clip is saved, so the UI can avoid
                    # offering a link to a file that isn't there yet.
                    "csvExists": os.path.exists(csv_path),
                    "captions": (
                        os.path.basename(self.server.caption_path)
                        if self.server.caption_path
                        else None
                    ),
                }
            )
        self._error(404, "not found")

    def do_HEAD(self):
        if urllib.parse.urlparse(self.path).path == "/video":
            return self._serve_video(head_only=True)
        self._error(404, "not found")

    def _serve_html(self):
        try:
            with open(HTML_PATH, "rb") as fh:
                body = fh.read()
        except OSError:
            return self._error(500, f"cannot read {HTML_PATH}")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_captions(self):
        """Serve the sidecar captions as WebVTT, converting from SRT if needed."""
        path = self.server.caption_path
        if not path:
            return self._error(404, "no caption file found")
        try:
            # Subtitle files are routinely latin-1 or cp1252; don't die on a
            # stray byte in a file we're only displaying.
            with open(path, encoding="utf-8-sig", errors="replace") as fh:
                text = fh.read()
        except OSError as exc:
            return self._error(500, f"could not read captions: {exc}")

        if not path.lower().endswith(".vtt"):
            text = srt_to_vtt(text)
        elif not text.lstrip("﻿").startswith("WEBVTT"):
            # A .vtt missing its required header won't parse in any browser.
            text = "WEBVTT\n\n" + text.lstrip("﻿").lstrip()

        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/vtt; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_416(self, size):
        self.send_response(416)
        self.send_header("Content-Range", f"bytes */{size}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _serve_video(self, head_only=False):
        path = self.server.video_path
        try:
            size = os.path.getsize(path)
        except OSError:
            return self._error(404, "video not found")

        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        range_header = self.headers.get("Range")

        if range_header:
            span = parse_range(range_header, size)
            if span is None:
                return self._send_416(size)
            start, end = span
            partial = True
        else:
            start, end, partial = 0, size - 1, False

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

        if head_only:
            return

        remaining = length
        try:
            with open(path, "rb") as fh:
                fh.seek(start)
                while remaining > 0:
                    chunk = fh.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            # Normal: the browser abandons a range mid-flight on every seek.
            pass

    # -- mutations --------------------------------------------------------

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/clips":
            return self._add_clip()
        if path == "/playback-error":
            return self._playback_error()
        if path == "/reveal":
            return self._reveal()
        self._error(404, "not found")

    def _reveal(self):
        """Open the video or CSV in the OS default application.

        Browsers block file:// navigation from an http:// page, so the footer
        links can't open anything on their own. The server can, and it already
        owns both paths — so only the two known keys are accepted here, never a
        client-supplied path.
        """
        data = self._read_json() or {}
        targets = {
            "video": self.server.video_path,
            "csv": self.server.store.csv_path,
            "folder": os.path.dirname(self.server.store.csv_path),
        }
        target = targets.get(str(data.get("what") or ""))
        if target is None:
            return self._error(400, "what must be one of: video, csv, folder")
        if not os.path.exists(target):
            return self._error(404, f"{os.path.basename(target)} does not exist yet")

        opener = {"darwin": ["open"], "win32": ["cmd", "/c", "start", ""]}.get(
            sys.platform, ["xdg-open"]
        )
        try:
            subprocess.Popen(
                opener + [target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            return self._error(500, f"could not open: {exc}")
        self._send_json({"ok": True, "opened": os.path.basename(target)})

    def _add_clip(self):
        data = self._read_json()
        if data is None:
            return self._error(400, "malformed JSON body")
        try:
            start = float(data["start"])
            end = float(data["end"])
        except (KeyError, TypeError, ValueError):
            return self._error(400, "start and end must be numbers")
        if not (end > start):
            return self._error(400, "end must be greater than start")
        label = str(data.get("label") or "").strip()

        try:
            index = self.server.store.add(start, end, label)
        except OSError as exc:
            return self._error(500, f"could not write CSV: {exc}")

        print(f"clip {index + 1}: {start:.3f} → {end:.3f}" + (f"  {label}" if label else ""))
        self._send_json({"index": index, "clips": self.server.store.as_json()}, status=201)

    def do_PATCH(self):
        index = self._clip_index()
        if index is None:
            return self._error(404, "not found")
        data = self._read_json()
        if data is None:
            return self._error(400, "malformed JSON body")

        kwargs = {}
        for key in ("start", "end"):
            if key in data and data[key] is not None:
                try:
                    kwargs[key] = float(data[key])
                except (TypeError, ValueError):
                    return self._error(400, f"{key} must be a number")
        if "label" in data:
            kwargs["label"] = str(data["label"] or "").strip()

        try:
            self.server.store.update(index, **kwargs)
        except IndexError:
            return self._error(404, f"no clip at index {index}")
        except ValueError as exc:
            return self._error(400, str(exc))
        except OSError as exc:
            return self._error(500, f"could not write CSV: {exc}")

        self._send_json({"clips": self.server.store.as_json()})

    def do_DELETE(self):
        index = self._clip_index()
        if index is None:
            return self._error(404, "not found")
        try:
            self.server.store.delete(index)
        except IndexError:
            return self._error(404, f"no clip at index {index}")
        except OSError as exc:
            return self._error(500, f"could not write CSV: {exc}")
        print(f"deleted clip {index + 1}")
        self._send_json({"clips": self.server.store.as_json()})

    def _playback_error(self):
        """The browser couldn't decode the video. Print the ffmpeg escape hatch.

        The server can't observe a codec failure itself, so the page tells us.
        """
        data = self._read_json() or {}
        detail = str(data.get("message") or "unknown decode error")
        src = self.server.video_path
        proxy = os.path.splitext(src)[0] + ".proxy.mp4"
        print(
            f"\n  ⚠  the browser could not play this file: {detail}\n"
            f"\n  Make a browser-friendly proxy, then run clipper against that:\n"
            f"\n    ffmpeg -i {shlex.quote(src)} -c:v libx264 -crf 23 -preset veryfast "
            f"-c:a aac {shlex.quote(proxy)}\n"
            f"\n  Timings marked against the proxy are valid for the original —\n"
            f"  the transcode preserves timestamps.\n",
            file=sys.stderr,
        )
        self._send_json({"ok": True})


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        """Swallow client disconnects; report everything else.

        The browser keeps several pooled connections open and drops them
        routinely — every video seek abandons an in-flight range request, and
        idle keep-alive sockets get reset on teardown. socketserver's default
        prints a full traceback for each one, which buries the actual clip log
        in noise. These are normal TCP lifecycle events, not failures.

        Real exceptions still print, so a genuine bug in a handler is not
        silently hidden.
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


def derive_csv_path(video_path):
    """movie.mp4 → movie.clips.csv, so two videos in one folder don't collide."""
    base, _ = os.path.splitext(video_path)
    return base + ".clips.csv"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Mark clips within a video file, appending each to a CSV."
    )
    parser.add_argument("video", help="path to the video file")
    parser.add_argument(
        "--csv",
        help="where to write clips (default: alongside the video, <name>.clips.csv)",
    )
    parser.add_argument(
        "--captions",
        help="caption file to display (default: <name>.vtt or <name>.srt if present)",
    )
    parser.add_argument("--port", type=int, default=0, help="port (default: a free one)")
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser")
    args = parser.parse_args(argv)

    # Unbuffered, so the clip log and any ffmpeg hint show up as they happen
    # rather than whenever the pipe flushes.
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except AttributeError:
        pass

    video_path = os.path.abspath(os.path.expanduser(args.video))
    if not os.path.isfile(video_path):
        parser.error(f"no such file: {video_path}")
    if not os.path.exists(HTML_PATH):
        parser.error(f"missing {HTML_PATH} — it must sit next to clipper.py")

    csv_path = (
        os.path.abspath(os.path.expanduser(args.csv)) if args.csv else derive_csv_path(video_path)
    )

    if args.captions:
        caption_path = os.path.abspath(os.path.expanduser(args.captions))
        if not os.path.isfile(caption_path):
            parser.error(f"no such caption file: {caption_path}")
    else:
        caption_path = find_captions(video_path)

    store = ClipStore(csv_path)

    # 127.0.0.1 only. This serves an arbitrary local file; it has no business
    # being reachable from the network.
    server = Server(("127.0.0.1", args.port), Handler)
    server.video_path = video_path
    server.store = store
    server.caption_path = caption_path

    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print("\n  clipper\n")
    print(f"  video  {video_path}")
    print(f"  csv    {csv_path}")
    if caption_path:
        print(f"  subs   {caption_path}")
    print(f"  url    {url}\n")
    print("  Ctrl-C to stop.\n")

    if not args.no_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
