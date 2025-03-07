import os
import glob
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime
from PIL import Image
from PIL.ExifTags import TAGS


def get_exif_datetime(image_path):
    """
    Return the EXIF DateTimeOriginal as a datetime object if available,
    otherwise fallback to file's last modification time.
    """
    try:
        img = Image.open(image_path)
        exif_data = img._getexif()
        if exif_data:
            for tag_id, value in exif_data.items():
                tag = TAGS.get(tag_id, tag_id)
                if tag in ["DateTimeOriginal", "DateTime"]:
                    # EXIF date format: "YYYY:MM:DD HH:MM:SS"
                    return datetime.strptime(value, "%Y:%m:%d %H:%M:%S")
    except:
        pass
    # Fallback: file modification time
    mtime = os.path.getmtime(image_path)
    return datetime.fromtimestamp(mtime)


def get_image_file_list(images_dir, order_file=None):
    """
    1. If order_file is provided, read filenames in that order.
    2. Otherwise, gather images in images_dir, sorted by EXIF or file mod time.
    """
    if order_file and os.path.exists(order_file):
        with open(order_file, "r", encoding="utf-8") as f:
            names = [line.strip() for line in f if line.strip()]
        file_paths = [os.path.join(images_dir, name) for name in names]
        file_paths = [fp for fp in file_paths if os.path.exists(fp)]
    else:
        # Gather all JPG/PNG
        exts = ["*.jpg", "*.jpeg", "*.png"]
        file_paths = []
        for ext in exts:
            file_paths.extend(glob.glob(os.path.join(images_dir, ext)))
        # Sort by EXIF or mod time
        file_paths.sort(key=lambda fp: get_exif_datetime(fp))
    return file_paths


def build_ken_burns_filter(
    duration, fps, start_zoom, end_zoom, border_size, border_color,
    slideshow_width, slideshow_height
):
    """
    Build a filter string for Ken Burns (zoompan) from start_zoom to end_zoom,
    plus optional border/pad.
      - duration : per-image duration (seconds)
      - fps      : frames per second
      - start_zoom, end_zoom : e.g. (1.0, zoom_factor) or (zoom_factor, 1.0)
      - border_size, border_color : controls the pad around the zoomed area
      - slideshow_width, slideshow_height : final output resolution
    """
    total_frames = int(duration * fps)
    
    # Zoom expression: from start_zoom to end_zoom across total_frames
    # 'on' is the current frame index in zoompan (0..d-1).
    zoom_expr = f"'{start_zoom} + ({end_zoom} - {start_zoom})*(on/{total_frames})'"
    
    # Keep image centered horizontally & vertically.
    x_expr = "'iw/2 - (iw/zoom/2)'"
    y_expr = "'ih/2 - (ih/zoom/2)'"
    
    # If we want a border inside a slideshow_width x slideshow_height frame:
    inner_w = slideshow_width - 2 * border_size
    inner_h = slideshow_height - 2 * border_size
    
    # Step 1: zoompan
    zoompan_part = (
        f"zoompan=z={zoom_expr}:x={x_expr}:y={y_expr}"
        f":d={total_frames}:s={inner_w}x{inner_h}"
    )
    
    # Step 2: pad if border_size > 0
    if border_size > 0:
        pad_part = (
            f"pad={slideshow_width}:{slideshow_height}:"
            f"{border_size}:{border_size}:{border_color}"
        )
        return f"{zoompan_part},{pad_part}"
    else:
        return zoompan_part


def generate_ken_burns_segments(
    images, tmp_dir, duration_per_image, fps, zoom_factor,
    border_size, border_color, slideshow_width, slideshow_height
):
    """
    Generate one MP4 segment per image with alternating Ken Burns zoom IN (even i)
    or zoom OUT (odd i). Returns (segment_paths, segment_lengths).
    """
    segment_paths = []
    segment_lengths = []  # in seconds
    
    for i, img_path in enumerate(images):
        seg_path = os.path.join(tmp_dir, f"segment_{i:03d}.mp4")
        
        # Decide whether this image zooms in or out
        if i % 2 == 0:
            # Even => zoom in 1.0 -> zoom_factor
            start_zoom, end_zoom = (1.0, zoom_factor)
        else:
            # Odd => zoom out zoom_factor -> 1.0
            start_zoom, end_zoom = (zoom_factor, 1.0)
        
        filter_str = build_ken_burns_filter(
            duration=duration_per_image,
            fps=fps,
            start_zoom=start_zoom,
            end_zoom=end_zoom,
            border_size=border_size,
            border_color=border_color,
            slideshow_width=slideshow_width,
            slideshow_height=slideshow_height
        )
        
        # Generate the segment (no fades, just pure Ken Burns movement)
        ffmpeg_cmd = [
            "ffmpeg",
            "-y",
            "-loop", "1",
            "-t", str(duration_per_image),
            "-i", img_path,
            "-vf", filter_str,
            "-r", str(fps),
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            seg_path
        ]
        subprocess.run(ffmpeg_cmd, check=True)
        
        segment_paths.append(seg_path)
        segment_lengths.append(duration_per_image)
    
    return segment_paths, segment_lengths


def build_xfade_filter(segment_count, segment_lengths, crossfade_duration, transition="fade"):
    """
    Build a filter_complex chain of xfade transitions among all segments.
    For N segments, we create N-1 crossfades in a chain. The final output label
    is [v(N-1)] if zero-based indexing is used (like [v1], [v2], etc.).
    
    Example for 3 segments:
      [0:v][1:v] xfade=transition=fade:duration=1:offset=2 [v1];
      [v1][2:v] xfade=transition=fade:duration=1:offset=3 [v2]
    """
    if segment_count <= 1:
        # No crossfade needed or only one segment
        return "", "[0:v]"
    
    lines = []
    current_label = "[0:v]"
    # Keep track of the total "running" length
    total_time = segment_lengths[0]
    
    for i in range(1, segment_count):
        offset = total_time - crossfade_duration
        out_label = f"[v{i}]"
        line = (
            f"{current_label}[{i}:v] xfade="
            f"transition={transition}:duration={crossfade_duration}:offset={offset}"
            f"{out_label}"
        )
        lines.append(line)
        current_label = out_label
        
        # Once crossfaded, the effective new total length is old total
        # plus next segment length minus crossfade overlap
        total_time += segment_lengths[i] - crossfade_duration
    
    filter_str = "; ".join(lines)
    final_label = current_label
    return filter_str, final_label


def create_slideshow(
    images_dir,
    output_file="slideshow.mp4",
    order_file=None,
    duration_per_image=3.0,
    fps=25,
    zoom_factor=1.1,
    border_size=0,
    border_color="black",
    slideshow_width=1920,
    slideshow_height=1080,
    crossfade_duration=1.0,
    crossfade_transition="fade"
):
    """
    Create a Ken Burns slideshow from images with CROSSFADE transitions.
    The Ken Burns effect alternates between zoom in (on even i) and zoom out (on odd i).
    """
    
    # Gather / sort images
    images = get_image_file_list(images_dir, order_file)
    if not images:
        print("No images found in", images_dir)
        return
    
    # Make a temp folder for intermediate clips
    with tempfile.TemporaryDirectory() as tmp_dir:
        # STEP 1: Generate Ken Burns segments, toggling zoom in/out
        segment_paths, segment_lengths = generate_ken_burns_segments(
            images=images,
            tmp_dir=tmp_dir,
            duration_per_image=duration_per_image,
            fps=fps,
            zoom_factor=zoom_factor,
            border_size=border_size,
            border_color=border_color,
            slideshow_width=slideshow_width,
            slideshow_height=slideshow_height
        )
        
        # If there's only one segment, skip crossfading
        if len(segment_paths) == 1:
            os.rename(segment_paths[0], output_file)
            print(f"Slideshow created with only one image: {output_file}")
            return
        
        # STEP 2: Crossfade them into one
        xfade_filter_str, final_label = build_xfade_filter(
            segment_count=len(segment_paths),
            segment_lengths=segment_lengths,
            crossfade_duration=crossfade_duration,
            transition=crossfade_transition
        )
        
        # Build the ffmpeg command with multiple inputs
        ffmpeg_cmd = [
            "ffmpeg",
            "-y"
        ]
        for seg_path in segment_paths:
            ffmpeg_cmd += ["-i", seg_path]
        
        if xfade_filter_str:
            ffmpeg_cmd += [
                "-filter_complex", xfade_filter_str,
                "-map", final_label,
                "-c:v", "libx264",
                "-pix_fmt", "yuv420p",
                output_file
            ]
        else:
            # Just copy if no crossfade filter
            ffmpeg_cmd += [
                "-c:v", "copy",
                output_file
            ]
        
        subprocess.run(ffmpeg_cmd, check=True)
        print(f"Slideshow created with alternating zoom-in/out crossfades: {output_file}")


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Create a Ken Burns slideshow with CROSSFADE transitions, alternating zoom in/out.")
    parser.add_argument("--images_dir", required=True, help="Path to the folder of images.")
    parser.add_argument("--order_file", default=None, help="Optional text file specifying image order.")
    parser.add_argument("--output_file", default="slideshow.mp4", help="Output video filename.")
    parser.add_argument("--duration_per_image", type=float, default=3.0, help="Seconds each image is shown.")
    parser.add_argument("--fps", type=int, default=25, help="FPS for Ken Burns segments.")
    parser.add_argument("--zoom_factor", type=float, default=1.1, help="Zoom factor for Ken Burns effect.")
    parser.add_argument("--border_size", type=int, default=0, help="Border in pixels around each clip.")
    parser.add_argument("--border_color", default="black", help="Border color, e.g. 'black' or '0xFFFFFF'.")
    parser.add_argument("--slideshow_width", type=int, default=1920, help="Final slideshow width.")
    parser.add_argument("--slideshow_height", type=int, default=1080, help="Final slideshow height.")
    parser.add_argument("--crossfade_duration", type=float, default=1.0,
                        help="Duration (seconds) of the crossfade between clips.")
    parser.add_argument("--crossfade_transition", default="fade",
                        help="Transition type for xfade (e.g. 'fade', 'wipeleft', 'circlecrop', etc.).")
    args = parser.parse_args()
    
    create_slideshow(
        images_dir=args.images_dir,
        output_file=args.output_file,
        order_file=args.order_file,
        duration_per_image=args.duration_per_image,
        fps=args.fps,
        zoom_factor=args.zoom_factor,
        border_size=args.border_size,
        border_color=args.border_color,
        slideshow_width=args.slideshow_width,
        slideshow_height=args.slideshow_height,
        crossfade_duration=args.crossfade_duration,
        crossfade_transition=args.crossfade_transition
    )
