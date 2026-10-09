"""Unit tests for clipper.py. Standard library only:

    python3 -m unittest
"""

import contextlib
import csv
import http.client
import io
import json
import os
import tempfile
import threading
import unittest

import clipper


def quiet():
    """Swallow ClipStore's "loaded N clips" chatter."""
    return contextlib.redirect_stdout(io.StringIO())


class ParseRangeTest(unittest.TestCase):
    def test_explicit_span(self):
        self.assertEqual(clipper.parse_range("bytes=0-99", 1000), (0, 99))

    def test_open_ended(self):
        self.assertEqual(clipper.parse_range("bytes=500-", 1000), (500, 999))

    def test_suffix(self):
        self.assertEqual(clipper.parse_range("bytes=-100", 1000), (900, 999))

    def test_suffix_longer_than_file(self):
        self.assertEqual(clipper.parse_range("bytes=-5000", 1000), (0, 999))

    def test_end_clamped_to_size(self):
        self.assertEqual(clipper.parse_range("bytes=900-5000", 1000), (900, 999))

    def test_surrounding_whitespace(self):
        self.assertEqual(clipper.parse_range("  bytes=0-1 ", 1000), (0, 1))

    def test_unsatisfiable(self):
        for header in (
            "bytes=1000-",     # starts at EOF
            "bytes=50-10",     # backwards
            "bytes=-0",        # zero-length suffix
            "bytes=-",         # nothing at all
            "bytes=0-1,5-9",   # multi-range unsupported
            "items=0-1",       # wrong unit
            "garbage",
        ):
            with self.subTest(header=header):
                self.assertIsNone(clipper.parse_range(header, 1000))

    def test_empty_file(self):
        self.assertIsNone(clipper.parse_range("bytes=0-", 0))


class SrtToVttTest(unittest.TestCase):
    def test_header_and_separator(self):
        srt = "1\n00:00:01,500 --> 00:00:03,250\nHello\n"
        vtt = clipper.srt_to_vtt(srt)
        self.assertTrue(vtt.startswith("WEBVTT\n\n"))
        self.assertIn("00:00:01.500 --> 00:00:03.250", vtt)
        self.assertIn("Hello", vtt)

    def test_crlf_normalised(self):
        vtt = clipper.srt_to_vtt("1\r\n00:00:01,000 --> 00:00:02,000\r\nHi\r\n")
        self.assertNotIn("\r", vtt)

    def test_bom_stripped(self):
        vtt = clipper.srt_to_vtt("﻿1\n00:00:01,000 --> 00:00:02,000\nHi\n")
        self.assertTrue(vtt.startswith("WEBVTT\n\n1\n"))

    def test_single_digit_hour_padded(self):
        vtt = clipper.srt_to_vtt("0:00:01,000 --> 0:00:02,000\n")
        self.assertIn("00:00:01.000 --> 00:00:02.000", vtt)

    def test_short_millis_padded_on_right(self):
        # ",5" is half a second, so it must become ".500", not ".005".
        vtt = clipper.srt_to_vtt("00:00:01,5 --> 00:00:02,25\n")
        self.assertIn("00:00:01.500 --> 00:00:02.250", vtt)


SAMPLE_VTT = """WEBVTT

NOTE this block has no timing and is ignored

1
00:00:01.000 --> 00:00:03.000 align:start
<v Alice>Hello &amp; <i>welcome</i>.

00:04.000 --> 00:06.000
Second cue
spans two lines

01:00:00.500 --> 01:00:02.000
An hour in
"""


class ParseVttCuesTest(unittest.TestCase):
    def test_parses_timings_and_text(self):
        self.assertEqual(
            clipper.parse_vtt_cues(SAMPLE_VTT),
            [
                (1.0, 3.0, ["Hello & welcome."]),
                (4.0, 6.0, ["Second cue", "spans two lines"]),
                (3600.5, 3602.0, ["An hour in"]),
            ],
        )

    def test_crlf(self):
        cues = clipper.parse_vtt_cues(SAMPLE_VTT.replace("\n", "\r\n"))
        self.assertEqual(len(cues), 3)
        self.assertEqual(cues[1][2], ["Second cue", "spans two lines"])

    def test_converted_srt(self):
        srt = "1\n00:00:01,500 --> 00:00:02,000\nFrom SRT\n\n2\n00:00:03,000 --> 00:00:04,000\nNext\n"
        self.assertEqual(
            clipper.parse_vtt_cues(clipper.srt_to_vtt(srt)),
            [(1.5, 2.0, ["From SRT"]), (3.0, 4.0, ["Next"])],
        )

    def test_empty_cue(self):
        self.assertEqual(clipper.parse_vtt_cues("WEBVTT\n\n00:01.000 --> 00:02.000\n"), [(1.0, 2.0, [])])


class CaptionsBetweenTest(unittest.TestCase):
    CUES = [
        (1.0, 3.0, ["one"]),
        (4.0, 6.0, ["two", "three"]),
        (7.0, 9.0, ["four"]),
    ]

    def test_inside(self):
        self.assertEqual(clipper.captions_between(self.CUES, 3.5, 6.5), "two three")

    def test_partial_overlap_includes_whole_cue(self):
        self.assertEqual(clipper.captions_between(self.CUES, 2.5, 7.5), "one two three four")

    def test_touching_boundaries_excluded(self):
        # A cue ending exactly at the in point, or starting exactly at the out
        # point, isn't heard in the clip.
        self.assertEqual(clipper.captions_between(self.CUES, 3.0, 7.0), "two three")

    def test_nothing(self):
        self.assertEqual(clipper.captions_between(self.CUES, 10, 20), "")
        self.assertEqual(clipper.captions_between([], 0, 20), "")

    def test_rolling_captions_deduplicated(self):
        rolling = [
            (0.0, 2.0, ["the quick brown"]),
            (2.0, 4.0, ["the quick brown", "fox jumps over"]),
            (4.0, 6.0, ["fox jumps over", "the lazy dog"]),
        ]
        self.assertEqual(
            clipper.captions_between(rolling, 0, 6),
            "the quick brown fox jumps over the lazy dog",
        )

    def test_whitespace_collapsed(self):
        self.assertEqual(clipper.captions_between([(0, 1, ["a  b\t c"])], 0, 1), "a b c")


class ReadCaptionsVttTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, data):
        path = os.path.join(self.tmp.name, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def test_srt_converted(self):
        path = self.write("a.srt", b"1\n00:00:01,000 --> 00:00:02,000\nHi\n")
        self.assertIn("00:00:01.000 --> 00:00:02.000", clipper.read_captions_vtt(path))

    def test_vtt_missing_header_fixed(self):
        path = self.write("a.vtt", b"00:01.000 --> 00:02.000\nHi\n")
        self.assertTrue(clipper.read_captions_vtt(path).startswith("WEBVTT\n\n"))

    def test_utf8_bom_and_bad_bytes_tolerated(self):
        path = self.write("a.vtt", b"\xef\xbb\xbfWEBVTT\n\n00:01.000 --> 00:02.000\ncaf\xe9\n")
        text = clipper.read_captions_vtt(path)
        self.assertTrue(text.startswith("WEBVTT"))
        self.assertIn("caf\ufffd", text)


class FindCaptionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.video = self.touch("movie.mp4")

    def tearDown(self):
        self.tmp.cleanup()

    def touch(self, name):
        path = os.path.join(self.dir, name)
        open(path, "w").close()
        return path

    def test_none(self):
        self.assertIsNone(clipper.find_captions(self.video))

    def test_exact_srt(self):
        srt = self.touch("movie.srt")
        self.assertEqual(clipper.find_captions(self.video), srt)

    def test_vtt_preferred_over_srt(self):
        self.touch("movie.srt")
        vtt = self.touch("movie.vtt")
        self.assertEqual(clipper.find_captions(self.video), vtt)

    def test_exact_name_beats_language_tagged(self):
        self.touch("movie.en.vtt")
        srt = self.touch("movie.srt")
        self.assertEqual(clipper.find_captions(self.video), srt)

    def test_language_tagged(self):
        tagged = self.touch("movie.en.srt")
        self.assertEqual(clipper.find_captions(self.video), tagged)

    def test_other_videos_captions_ignored(self):
        self.touch("movie2.srt")
        self.touch("other.movie.srt")
        self.assertIsNone(clipper.find_captions(self.video))


class DeriveCsvPathTest(unittest.TestCase):
    def test_replaces_extension(self):
        self.assertEqual(clipper.derive_csv_path("/a/b/movie.mp4"), "/a/b/movie.clips.csv")

    def test_dotted_name(self):
        self.assertEqual(clipper.derive_csv_path("/a/my.movie.mkv"), "/a/my.movie.clips.csv")


class ClipStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.csv_path = os.path.join(self.tmp.name, "movie.clips.csv")

    def tearDown(self):
        self.tmp.cleanup()

    def store(self):
        with quiet():
            return clipper.ClipStore(self.csv_path)

    def rows(self):
        with open(self.csv_path, newline="", encoding="utf-8") as fh:
            return list(csv.reader(fh))

    def test_no_file_until_first_clip(self):
        store = self.store()
        self.assertEqual(store.as_json(), [])
        self.assertFalse(os.path.exists(self.csv_path))

    def test_add_writes_csv(self):
        store = self.store()
        store.add(92.4, 101.2, "Opening line")
        store.add(254, 258.5, "")
        self.assertEqual(
            self.rows(),
            [
                ["start", "end", "duration", "label", "captions"],
                ["92.400", "101.200", "8.800", "Opening line", ""],
                ["254.000", "258.500", "4.500", "", ""],
            ],
        )
        self.assertFalse(os.path.exists(self.csv_path + ".tmp"))

    def test_as_json_rounds_duration(self):
        store = self.store()
        store.add(92.4, 101.2, "")
        self.assertEqual(store.as_json()[0]["duration"], 8.8)

    def test_label_with_comma_and_quote_round_trips(self):
        label = 'He said, "hi"'
        self.store().add(1, 2, label)
        self.assertEqual(self.store().as_json()[0]["label"], label)

    def test_reload_and_backup(self):
        self.store().add(1, 2, "first")
        with open(self.csv_path, "rb") as fh:
            original = fh.read()

        store = self.store()
        self.assertEqual([c["label"] for c in store.as_json()], ["first"])
        with open(self.csv_path + ".bak", "rb") as fh:
            self.assertEqual(fh.read(), original)

    def test_load_skips_unparseable_rows(self):
        with open(self.csv_path, "w", encoding="utf-8") as fh:
            fh.write("start,end,duration,label\n1,2,1,ok\noops,3,,bad\n4,5,1,  padded  \n")
        clips = self.store().as_json()
        self.assertEqual([c["label"] for c in clips], ["ok", "padded"])

    def test_update(self):
        store = self.store()
        store.add(1, 2, "a")
        store.update(0, end=3, label="b")
        self.assertEqual(
            store.as_json()[0],
            {"start": 1, "end": 3, "duration": 2, "label": "b", "captions": ""},
        )
        self.assertEqual(self.rows()[1], ["1.000", "3.000", "2.000", "b", ""])

    def test_update_captions_only(self):
        store = self.store()
        store.add(1, 2, "a", "old")
        store.update(0, captions="new")
        self.assertEqual(store.as_json()[0]["captions"], "new")
        self.assertEqual(store.as_json()[0]["label"], "a")

    def test_multiline_captions_round_trip(self):
        captions = 'Line one, "quoted"\nLine two'
        self.store().add(1, 2, "", captions)
        self.assertEqual(self.store().as_json()[0]["captions"], captions)

    def test_loads_csv_without_captions_column(self):
        with open(self.csv_path, "w", encoding="utf-8") as fh:
            fh.write("start,end,duration,label\n1,2,1,old\n")
        store = self.store()
        self.assertEqual(store.as_json()[0]["captions"], "")
        store.add(3, 4, "new", "hi")
        self.assertEqual(self.rows()[0], clipper.FIELDS)
        self.assertEqual(self.rows()[1], ["1.000", "2.000", "1.000", "old", ""])

    def test_update_rejects_inverted_span_without_changing_anything(self):
        store = self.store()
        store.add(1, 2, "a")
        with self.assertRaises(ValueError):
            store.update(0, start=5)
        self.assertEqual(store.as_json()[0]["start"], 1)

    def test_update_and_delete_out_of_range(self):
        store = self.store()
        with self.assertRaises(IndexError):
            store.update(0, label="x")
        with self.assertRaises(IndexError):
            store.delete(0)

    def test_delete(self):
        store = self.store()
        store.add(1, 2, "a")
        store.add(3, 4, "b")
        store.delete(0)
        self.assertEqual([c["label"] for c in store.as_json()], ["b"])
        self.assertEqual(len(self.rows()), 2)


class QuietHandler(clipper.Handler):
    def log_message(self, fmt, *args):
        pass


class ServerFixture(unittest.TestCase):
    """A real server on an ephemeral port, serving a 1 KB fake video."""

    VIDEO_BYTES = bytes(range(256)) * 4   # 1024 bytes

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.video = os.path.join(self.tmp.name, "movie.mp4")
        with open(self.video, "wb") as fh:
            fh.write(self.VIDEO_BYTES)
        self.csv_path = clipper.derive_csv_path(self.video)

        with quiet():
            store = clipper.ClipStore(self.csv_path)
        self.server = clipper.Server(("127.0.0.1", 0), QuietHandler)
        self.server.video_path = self.video
        self.server.store = store
        self.server.caption_path = None
        self.thread = threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        headers = dict(headers or {})
        if body is not None:
            body = json.dumps(body)
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=body, headers=headers)
        res = conn.getresponse()
        data = res.read()
        conn.close()
        return res, data

    def request_json(self, method, path, body=None):
        res, data = self.request(method, path, body)
        return res.status, json.loads(data)


class ServerTest(ServerFixture):
    """End-to-end through real HTTP, with no caption file."""

    def test_full_video(self):
        res, data = self.request("GET", "/video")
        self.assertEqual(res.status, 200)
        self.assertEqual(data, self.VIDEO_BYTES)
        self.assertEqual(res.getheader("Accept-Ranges"), "bytes")
        self.assertEqual(res.getheader("Content-Type"), "video/mp4")

    def test_range_request(self):
        res, data = self.request("GET", "/video", headers={"Range": "bytes=10-19"})
        self.assertEqual(res.status, 206)
        self.assertEqual(data, self.VIDEO_BYTES[10:20])
        self.assertEqual(res.getheader("Content-Range"), "bytes 10-19/1024")

    def test_unsatisfiable_range(self):
        res, _ = self.request("GET", "/video", headers={"Range": "bytes=5000-"})
        self.assertEqual(res.status, 416)
        self.assertEqual(res.getheader("Content-Range"), "bytes */1024")

    def test_no_captions(self):
        res, _ = self.request("GET", "/captions")
        self.assertEqual(res.status, 404)

    def test_meta(self):
        status, meta = self.request_json("GET", "/meta")
        self.assertEqual(status, 200)
        self.assertEqual(meta["video"], "movie.mp4")
        self.assertEqual(meta["csv"], "movie.clips.csv")
        self.assertFalse(meta["csvExists"])
        self.assertIsNone(meta["captions"])

    def test_clip_lifecycle(self):
        with quiet():
            status, res = self.request_json("POST", "/clips", {"start": 1, "end": 2.5, "label": " x "})
        self.assertEqual(status, 201)
        self.assertEqual(res["index"], 0)
        self.assertEqual(
            res["clips"],
            [{"start": 1, "end": 2.5, "duration": 1.5, "label": "x", "captions": ""}],
        )
        self.assertTrue(os.path.exists(self.csv_path))

        status, res = self.request_json("PATCH", "/clips/0", {"label": "renamed"})
        self.assertEqual(status, 200)
        self.assertEqual(res["clips"][0]["label"], "renamed")

        with quiet():
            status, res = self.request_json("DELETE", "/clips/0")
        self.assertEqual(status, 200)
        self.assertEqual(res["clips"], [])

    def test_add_rejects_bad_input(self):
        for body in ({"start": 5, "end": 5}, {"start": "a", "end": 2}, {"end": 2}):
            with self.subTest(body=body):
                status, res = self.request_json("POST", "/clips", body)
                self.assertEqual(status, 400)
                self.assertIn("error", res)
        self.assertFalse(os.path.exists(self.csv_path))

    def test_malformed_json(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        conn.request("POST", "/clips", body="{not json", headers={"Content-Type": "application/json"})
        res = conn.getresponse()
        self.assertEqual(res.status, 400)
        self.assertEqual(json.loads(res.read()), {"error": "malformed JSON body"})
        conn.close()

    def test_patch_and_delete_missing_clip(self):
        status, _ = self.request_json("PATCH", "/clips/3", {"label": "x"})
        self.assertEqual(status, 404)
        status, _ = self.request_json("DELETE", "/clips/3")
        self.assertEqual(status, 404)

    def test_reveal_rejects_unknown_target(self):
        status, _ = self.request_json("POST", "/reveal", {"what": "/etc/passwd"})
        self.assertEqual(status, 400)

    def test_reveal_missing_csv(self):
        status, _ = self.request_json("POST", "/reveal", {"what": "csv"})
        self.assertEqual(status, 404)

    def test_unknown_path(self):
        status, _ = self.request_json("GET", "/nope")
        self.assertEqual(status, 404)

    def test_caption_text_without_caption_file(self):
        status, _ = self.request_json("GET", "/caption-text?start=0&end=5")
        self.assertEqual(status, 404)
        # Saving still works; captions are simply empty unless supplied.
        with quiet():
            status, res = self.request_json("POST", "/clips", {"start": 0, "end": 5})
        self.assertEqual(status, 201)
        self.assertEqual(res["clips"][0]["captions"], "")


class CaptionServerTest(ServerFixture):
    """The same server, with a caption file beside the video."""

    def setUp(self):
        super().setUp()
        path = os.path.join(self.tmp.name, "movie.vtt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(SAMPLE_VTT)
        self.server.caption_path = path

    def save(self, body):
        with quiet():
            status, res = self.request_json("POST", "/clips", body)
        self.assertEqual(status, 201)
        return res["clips"][res["index"]]

    def test_serves_vtt(self):
        res, data = self.request("GET", "/captions")
        self.assertEqual(res.status, 200)
        self.assertTrue(data.startswith(b"WEBVTT"))

    def test_caption_text_preview(self):
        status, res = self.request_json("GET", "/caption-text?start=2&end=5")
        self.assertEqual(status, 200)
        self.assertEqual(res, {"text": "Hello & welcome. Second cue spans two lines"})

    def test_caption_text_bad_query(self):
        for query in ("", "?start=1", "?start=a&end=2"):
            with self.subTest(query=query):
                status, _ = self.request_json("GET", "/caption-text" + query)
                self.assertEqual(status, 400)

    def test_unedited_captions_extracted_on_save(self):
        # null (and absent) mean "not edited": the server fills them in.
        self.assertEqual(self.save({"start": 0, "end": 2, "captions": None})["captions"],
                         "Hello & welcome.")
        self.assertEqual(self.save({"start": 4.5, "end": 5})["captions"],
                         "Second cue spans two lines")
        with open(self.csv_path, newline="", encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
        self.assertEqual(rows[1][4], "Hello & welcome.")

    def test_edited_captions_kept_verbatim(self):
        clip = self.save({"start": 0, "end": 2, "captions": "  My own\r\nwords  "})
        self.assertEqual(clip["captions"], "My own\nwords")

    def test_edited_empty_captions_kept_empty(self):
        self.assertEqual(self.save({"start": 0, "end": 2, "captions": ""})["captions"], "")

    def test_patch_captions(self):
        self.save({"start": 0, "end": 2})
        status, res = self.request_json("PATCH", "/clips/0", {"captions": "fixed"})
        self.assertEqual(status, 200)
        self.assertEqual(res["clips"][0]["captions"], "fixed")

    def test_patch_null_captions_reextracts_for_new_span(self):
        self.save({"start": 0, "end": 2, "captions": "manual"})
        status, res = self.request_json("PATCH", "/clips/0", {"end": 5, "captions": None})
        self.assertEqual(status, 200)
        self.assertEqual(res["clips"][0]["captions"], "Hello & welcome. Second cue spans two lines")


if __name__ == "__main__":
    unittest.main()
