# Things taught

Newest first. Plain everyday words, nothing dumbed down.

## 2026-10-10 — Longer lead-in before each kept part

Kept parts now start 8 seconds before the first dog sighting instead of 3,
so entrances are never cut; the tail stays at 3. One knob became two
(`--handles-before` / `--handles-after`) so each side tunes alone;
`--handles` still sets both at once. Applies to new analyzes only — old
review lists keep their baked-in edges unless re-run.

## 2026-10-10 — Dog detector misses back-of-head, nose-down, half-out dogs

A clear side view scores ~0.55 and is kept. Back of the head with nose down
sniffing, or a dog half out of the picture at the frame edge, scores 0.00
and is deleted — same family as the sleeping-in-a-bed miss. Lowering the
keep threshold was rejected (it floods the list with junk). The fix keeps
review as the safety net but tools it up: analyze now saves every frame's
score with its file name (`hits.csv`) and sample pictures for each long
deleted stretch (`gaps/` + `deleted.csv`, tuned with `--gap-review`).

## 2026-10-08 — Dog detector sees clear dogs, not sleeping-in-house

The YOLO dog check looks for a clear dog shape out in the open. It skips
a puppy curled dark inside the wicker house (frames 003296 and 006581).
That is why two long blocks read as deleted even though she is there.
Rule we chose: keep active dog, cut house-sleeping and empty room as not
interesting. If the rule flips to keep every second on screen, those blocks
must be added back by hand and the file grows by ~2 hours.

## 2026-10-05 — Check sound by decoding, never by listing tracks

A file can list a sound track and still play silence. Twice now the probe
showed `aac` while decoding gave zero bytes. From here: a sound claim counts
only when raw bytes come out of the decoder (check size, RMS, peak). The
cause both times sat one step earlier — first sound skipped as “invalid
data”, then sound starved by `-shortest` under timestamp-less picture copy.
Also: copy the picture all you like, but cut with a measured `-t`, never
`-shortest`, when inputs carry no real timestamps.

## 2026-10-05 — Small pieces beat one giant video, if names carry time

Joining 1000+ clips in one go is slow and one bad clip can spoil hours. The
better shape is: convert each motion event to its own short MP4 (fast,
isolated failures), then copy-join the pieces (minutes, no quality loss since nothing
is re-drawn). For the join to come out right, filenames must sort in time
order — ours do (`09-24-1632510909_0015` embeds month, day, and event time),
and the join window still shows the full order with Time/Original/Up/Down so
a reversed list can never sneak through. Folders named `2021 video` count as
year 2021 now, because the scanner looks at the first 4 digits, not the
whole name.

## 2026-10-05 — Some camera files hide sound behind notes

Your LittlelfSmart `.media` files hold picture and sound mixed together. Each
chunk starts with a 24-byte note: what kind it is, how long it is, and the
time in milliseconds. Type 0 and 1 mean picture (H.264 video). Type 3 means
sound (640 bytes = 40ms of 8000Hz 16-bit mono sound).

Normal video tools look for picture start codes (`00 00 00 01`) and skip the
rest as bad data. That is why `ffprobe` showed video-only and the old app made
silent video — it gave the whole file to FFmpeg, which dropped the type-3
chunks. The fix reads the 24-byte notes first, joins all picture parts to
`video.h264` and all sound parts to `audio.raw`, then gives both to FFmpeg so
the output has two tracks (H.264 + AAC).

Two follow-on lessons: the last flag word in the note varies per camera (seen
20, 24, 81), so it must not be used to accept or reject files — only the
24-byte size, sane lengths, and types 0/1/3 gate. And bulk-joining hundreds of
clips drifts because each clip runs its own clock (measured 15-20 fps); the
fix encodes each clip on its own measured rate, then joins the finished
segments with copy, keeping sound lined up with picture.
