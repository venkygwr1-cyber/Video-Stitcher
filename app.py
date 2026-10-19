import math
import os
import subprocess
import tempfile

import streamlit as st

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


uploaded = st.file_uploader(
    "Select your videos", type=["mp4", "mov", "mkv", "avi", "webm"],
    accept_multiple_files=True,
)

col1, col2 = st.columns(2)
seg_len = col1.number_input("Seconds per clip", min_value=1, max_value=300, value=15)
resolution = col2.selectbox("Output size", ["1280x720 (landscape)", "720x1280 (portrait)", "1920x1080 (landscape)"])
sort_names = st.checkbox("Sort videos by file name", value=True)

if st.button("Stitch videos", type="primary", disabled=not uploaded or len(uploaded) < 2):
    width, height = map(int, resolution.split(" ")[0].split("x"))
    files = sorted(uploaded, key=lambda f: f.name) if sort_names else list(uploaded)

    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for i, f in enumerate(files):
            p = os.path.join(tmp, f"src_{i}{os.path.splitext(f.name)[1]}")
            with open(p, "wb") as out:
                out.write(f.getbuffer())
            paths.append(p)

        durations = [probe_duration(p) for p in paths]
        rounds = max(math.ceil(d / seg_len) for d in durations)

        jobs = []
        for r in range(rounds):
            for j, p in enumerate(paths):
                start = r * seg_len
                if start >= durations[j]:
                    continue
                # skip a tiny leftover tail (<0.5s) to avoid glitchy clips
                if durations[j] - start < 0.5:
                    continue
                jobs.append((p, start, os.path.join(tmp, f"seg_{r:03d}_{j}.mp4")))

        progress = st.progress(0.0, text="Starting...")
        segs = []
        try:
            for k, (p, start, outp) in enumerate(jobs):
                cut_segment(p, start, seg_len, outp, width, height)
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
