import math
import os
import subprocess
import tempfile

import streamlit as st

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

st.set_page_config(page_title="Video Interleaver", page_icon="🎬")
st.title("🎬 Video Interleaver")
st.caption("Takes N seconds from each video in turn, round-robin, and stitches them into one file.")

# ---- Optional password (set APP_PASSWORD in Streamlit/HF secrets) ----
password = os.environ.get("APP_PASSWORD")
if not password:
    try:
        password = st.secrets.get("APP_PASSWORD")
    except Exception:
        password = None

if password:
    entered = st.text_input("Password", type="password")
    if entered != password:
        st.stop()


def probe_duration(path):
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", path]
    )
    return float(out.decode().strip())


def has_audio(path):
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=index", "-of", "csv=p=0", path]
    )
    return bool(out.strip())


def cut_segment(src, start, length, out_path, width, height):
    vf = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
          f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps=30,setsar=1")
    cmd = ["ffmpeg", "-y", "-ss", str(start), "-t", str(length), "-i", src]
    if not has_audio(src):
        # add silent audio so every segment has the same streams
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-shortest"]
    cmd += ["-vf", vf, "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
            "-c:a", "aac", "-ar", "44100", "-ac", "2", out_path]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def download_video(url, folder, idx, on_progress):
    """Download one video link (max 720p) into `folder`; returns the file path."""
    opts = {
        "format": "bv*[height<=720]+ba/b[height<=720]/b",
        "merge_output_format": "mp4",
        "outtmpl": os.path.join(folder, f"dl_{idx}.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [on_progress],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    for name in sorted(os.listdir(folder)):
        if name.startswith(f"dl_{idx}.") and not name.endswith((".part", ".ytdl")):
            return os.path.join(folder, name)
    raise RuntimeError("Download finished but no file was found.")


source = st.radio(
    "Where are your videos?",
    ["Upload from my device", "Paste YouTube links", "Both"],
    horizontal=True,
)

uploaded = []
if source in ("Upload from my device", "Both"):
    uploaded = st.file_uploader(
        "Select your videos", type=["mp4", "mov", "mkv", "avi", "webm"],
        accept_multiple_files=True,
    )

links = []
if source in ("Paste YouTube links", "Both"):
    links_text = st.text_area(
        "Paste video links (YouTube etc.), one per line",
        placeholder="https://www.youtube.com/watch?v=...\nhttps://youtu.be/...",
        help="Only use videos you own or have permission to reuse. Downloaded at up to 720p.",
    )
    links = [ln.strip() for ln in links_text.splitlines() if ln.strip().lower().startswith("http")]
if links:
    if yt_dlp is None:
        st.error("Link downloads need 'yt-dlp' in requirements.txt. Add it and redeploy.")
    else:
        st.caption(f"🔗 {len(links)} link(s) added. They are downloaded when you tap Stitch.")
n_uploaded = len(uploaded) if uploaded else 0
total_sources = n_uploaded + len(links)


def fmt_time(seconds):
    m, s = divmod(int(round(seconds)), 60)
    return f"{m}:{s:02d}"


# ---- "Ready" summary: shows each uploaded video once it has fully arrived ----
if uploaded:
    cache = st.session_state.setdefault("duration_cache", {})
    lines = []
    total_bytes = 0
    for f in uploaded:
        key = getattr(f, "file_id", None) or (f.name, f.size)
        if key not in cache:
            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    suffix=os.path.splitext(f.name)[1], delete=False
                ) as t:
                    t.write(f.getbuffer())
                    tmp_path = t.name
                cache[key] = probe_duration(tmp_path)
            except Exception:
                cache[key] = None
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    os.remove(tmp_path)
        dur = cache[key]
        total_bytes += f.size
        dur_txt = fmt_time(dur) if dur else "length unknown"
        lines.append(f"✅ **{f.name}** — {f.size / 1e6:.1f} MB · {dur_txt}")
    st.success(f"{len(uploaded)} video(s) fully uploaded · {total_bytes / 1e6:.1f} MB total")
    st.markdown("\n\n".join(lines))
elif not links:
    st.info("No videos added yet.")

col1, col2 = st.columns(2)
seg_len = col1.number_input("Seconds per clip", min_value=1, max_value=300, value=15)
resolution = col2.selectbox("Output size", ["1280x720 (landscape)", "720x1280 (portrait)", "1920x1080 (landscape)"])
mode = st.radio(
    "Stitching pattern",
    [
        "Rotate through videos along the timeline (clip 1 from video 1, clip 2 from video 2, clip 3 from video 3, ... then back to video 1)",
        "Each video in turn (first clip of every video, then the next clip of every video, ...)",
    ],
)
sort_names = st.checkbox("Sort videos by file name", value=True)

# ---- Final video order: uploads first (sorted if ticked), then links ----
up_files = list(uploaded) if uploaded else []
files = sorted(up_files, key=lambda f: f.name) if sort_names else up_files

entries = []  # (stable key, label) in the order the videos will be used
for i, f in enumerate(files):
    fid = getattr(f, "file_id", None) or f"{f.name}-{f.size}-{i}"
    entries.append((f"up_{fid}", f.name))
for k, url in enumerate(links):
    short = url if len(url) <= 55 else url[:52] + "..."
    entries.append((f"lk_{k}_{url}", f"Link {k + 1}: {short}"))

# ---- Cut amounts for each video (boxes follow the video if you re-sort) ----
trims = []  # (cut_from_start, cut_from_end), same order as `entries`
if entries:
    st.markdown("**Cut from each video (seconds)**")
    for idx, (key, label) in enumerate(entries):
        st.markdown(f"{idx + 1}. {label}")
        c_start, c_end = st.columns(2)
        t_s = c_start.number_input(
            "From start", min_value=0.0, max_value=3600.0, value=0.0, step=1.0, key=f"ts_{key}")
        t_e = c_end.number_input(
            "From end", min_value=0.0, max_value=3600.0, value=0.0, step=1.0, key=f"te_{key}")
        trims.append((t_s, t_e))

can_run = total_sources >= 2 and (yt_dlp is not None or not links)
st.caption("Order: uploaded videos first, then links in the order you pasted them.")
if st.button("Stitch videos", type="primary", disabled=not can_run):
    width, height = map(int, resolution.split(" ")[0].split("x"))
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for i, f in enumerate(files):
            p = os.path.join(tmp, f"src_{i}{os.path.splitext(f.name)[1]}")
            with open(p, "wb") as out:
                out.write(f.getbuffer())
            paths.append(p)

        if links:
            dl_bar = st.progress(0.0, text="Downloading links...")
            for k, url in enumerate(links):
                def on_progress(d, k=k):
                    if d.get("status") == "downloading":
                        total = d.get("total_bytes") or d.get("total_bytes_estimate")
                        frac = (d.get("downloaded_bytes", 0) / total) if total else 0.0
                        overall = min((k + min(frac, 1.0)) / len(links), 1.0)
                        dl_bar.progress(
                            overall,
                            text=f"Downloading link {k + 1} of {len(links)} ({frac * 100:.0f}%)",
                        )
                try:
                    paths.append(download_video(url, tmp, k, on_progress))
                except Exception as e:
                    st.error(
                        f"Could not download link {k + 1}: {url}\n\n"
                        "The video may be private, age-restricted or removed. On a free cloud "
                        "server YouTube can also block the download as a bot check. "
                        "Try again later, or run the app on your own computer."
                    )
                    st.caption(f"Details: {str(e)[:300]}")
                    st.stop()
            dl_bar.empty()

        durations = [probe_duration(p) for p in paths]
        n = len(paths)
        # usable length of each video after cutting the start and end
        usable = [max(d - trims[j][0] - trims[j][1], 0.0) for j, d in enumerate(durations)]
        for j, u in enumerate(usable):
            if u < 0.5:
                st.warning(f"Video {j + 1} is too short after trimming, so it is skipped.")
        slots = max(math.ceil(u / seg_len) for u in usable)

        jobs = []

        def add_job(j, offset):
            # offset = seconds into the trimmed video. Skip if it has ended,
            # or only a tiny tail (<0.5s) is left
            if offset >= usable[j] or usable[j] - offset < 0.5:
                return
            length = min(seg_len, usable[j] - offset)
            jobs.append((paths[j], trims[j][0] + offset, length,
                         os.path.join(tmp, f"seg_{len(jobs):05d}.mp4")))

        if mode.startswith("Rotate"):
            # clip 1 from video 1 (0-15s), clip 2 from video 2 (15-30s),
            # clip 3 from video 3 (30-45s) ... then back to video 1, and so on
            for g in range(slots * n):
                add_job(g % n, g * seg_len)
        else:
            # first clip of every video, then the next clip of every video, ...
            for r in range(slots):
                for j in range(n):
                    add_job(j, r * seg_len)

        if not jobs:
            st.error("No usable footage left. Try cutting less from the start or end.")
            st.stop()

        progress = st.progress(0.0, text="Starting...")
        segs = []
        try:
            for k, (p, start, length, outp) in enumerate(jobs):
                cut_segment(p, start, length, outp, width, height)
                segs.append(outp)
                progress.progress((k + 1) / len(jobs), text=f"Cutting clip {k + 1} of {len(jobs)}")

            list_path = os.path.join(tmp, "list.txt")
            with open(list_path, "w") as lf:
                for s in segs:
                    lf.write(f"file '{s}'\n")

            final = os.path.join(tmp, "final.mp4")
            progress.progress(1.0, text="Joining clips...")
            subprocess.run(
                ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path,
                 "-c", "copy", "-movflags", "+faststart", final],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            with open(final, "rb") as fh:
                data = fh.read()
        except subprocess.CalledProcessError:
            st.error("FFmpeg failed. One of the files may be corrupt or unsupported.")
            st.stop()

    progress.empty()
    st.success(f"Done: {len(segs)} clips stitched ({len(data) / 1e6:.1f} MB).")
    st.download_button("Download stitched video", data, file_name="stitched.mp4", mime="video/mp4")
