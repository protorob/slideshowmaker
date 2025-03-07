# slideshowmaker
Picture Slideshow Maker with Python and FFMpeg

## How to use it?

Here are the flags:

python crossfade_slideshow.py \
    --images_dir ./images \
    --order_file ./order.txt \
    --output_file my_slideshow.mp4 \
    --duration_per_image 5 \
    --fps 30 \
    --zoom_factor 1.2 \
    --border_size 20 \
    --border_color 0xFF0000 \
    --slideshow_width 1280 \
    --slideshow_height 720 \
    --crossfade_duration 1.5 \
    --crossfade_transition circlecrop