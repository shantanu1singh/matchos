"""Prepare exact excerpts, source icons and trimmed recording clips."""
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
source = (ROOT.parents[1] / 'matchos/web/app.js').read_text()
for name, color in {'broadcaster':'#bc9be8','fan':'#e94c55','analyst':'#e6c36a'}.items():
    shapes = re.search(name + r": '(.*?)'", source)[1]
    (ROOT / f'assets/persona-{name}.svg').write_text(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">{shapes}</svg>')
subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-i',str(ROOT/'capture/screenshots/press.png'),
                '-vf','crop=250:46:18:14','-frames:v','1',str(ROOT/'assets/brand.png')],check=True)
print('Prepared three original app icons and captured brand image.')
