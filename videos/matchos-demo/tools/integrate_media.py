"""Use the recorded judge checkpoints; extend held UI frames to scene length."""
from pathlib import Path
import re
import subprocess

ROOT=Path(__file__).resolve().parents[1]
for beat,name in [(3,'goal'),(4,'card')]:
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-i',str(ROOT/f'assets/{name}.mp4'),
        '-vf','tpad=stop_mode=clone:stop_duration=5','-t','10.45','-an','-c:v','libx264','-crf','18','-pix_fmt','yuv420p',str(ROOT/f'assets/{name}-hold.mp4')],check=True)
    p=ROOT/f'compositions/beat-{beat}.html'
    text=p.read_text()
    text=re.sub(r'<img src="\.\./capture/screenshots/'+name+r'\.png"[^>]*>',
                f'<video id="b{beat}-source-video" class="clip" src="../assets/{name}-hold.mp4" data-start="0" data-duration="10.45" data-track-index="2" muted playsinline></video>',text)
    text=text.replace(f'.b{beat}-screen img{{',f'.b{beat}-screen img,#beat-{beat} .b{beat}-screen video{{')
    p.write_text(text)
print('Integrated real goal and dismissal footage; held frames extended explicitly.')
