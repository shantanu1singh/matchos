"""Local Kokoro narration, exact 120-second mix and honest measured timing map."""
import json
from pathlib import Path
import re
import subprocess
import shutil

import numpy as np
import soundfile as sf
from kokoro_onnx import Kokoro

ROOT = Path(__file__).resolve().parents[1]
sections = re.split(r'^## .*$', (ROOT / 'SCRIPT.md').read_text(), flags=re.M)[1:]
texts = [s.strip() for s in sections]
slots = [(x['scene_start'],x['scene_end']) for x in json.loads((ROOT/'narration-plan.json').read_text())]
(ROOT/'narration.txt').write_text('\n\n'.join(texts)+'\n')
model = Kokoro(str(Path.home()/'.cache/hyperframes/tts/models/kokoro-v1.0.onnx'),
               str(Path.home()/'.cache/hyperframes/tts/voices/voices-v1.0.bin'))
rate = 24000
mix = np.zeros(120*rate, dtype=np.float32)
timings = []
for i,(text,(start,end)) in enumerate(zip(texts,slots),1):
    raw = ROOT/f'assets/vo-{i}-raw.wav'
    final = ROOT/f'assets/vo-{i}.wav'
    audio, sample_rate = model.create(text,voice='af_nova',speed=1.0,lang='en-us')
    assert sample_rate==rate
    original_duration=len(audio)/rate
    available=end-start-1.05
    tempo=max(.92,original_duration/available)
    sf.write(raw,audio,rate)
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-i',str(raw),
        '-af',f'atempo={tempo:.7f},afade=t=in:d=0.015',str(final)],check=True)
    samples, sr = sf.read(final,dtype='float32')
    duration = len(samples)/sr
    offset = start+.6
    assert duration < end-offset-.15, (i,duration,available)
    mix[round(offset*rate):round(offset*rate)+len(samples)] = samples
    timings.append({'scene':i,'scene_start':start,'scene_end':end,'speech_start':offset,'speech_end':round(offset+duration,3),
                    'text':text,'voice':'af_nova','original_duration':round(original_duration,3),'tempo':round(tempo,4)})
    print(f'Scene {i}: speech {offset:.2f}–{offset+duration:.2f}s; tempo {tempo:.3f}',flush=True)
sf.write(ROOT/'narration.wav',mix,rate)
# Master speech to a consistent playback level, retaining the exact timeline.
analysis = subprocess.run(['ffmpeg','-hide_banner','-i',str(ROOT/'narration.wav'),
    '-af','loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json','-f','null','-'],
    capture_output=True,text=True,check=True)
levels = json.loads(re.search(r'\{[^{}]+\}',analysis.stderr,re.S).group())
master = ROOT/'.hyperframes/narration-master.wav'
master.parent.mkdir(exist_ok=True)
filt = ('loudnorm=I=-16:TP=-1.5:LRA=11:linear=false:'
        f"measured_I={levels['input_i']}:measured_TP={levels['input_tp']}:"
        f"measured_LRA={levels['input_lra']}:measured_thresh={levels['input_thresh']}:"
        f"offset={levels['target_offset']}")
subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-i',str(ROOT/'narration.wav'),
    '-af',filt,'-ar',str(rate),'-ac','1','-t','120',str(master)],check=True)
master.replace(ROOT/'narration.wav')
(ROOT/'timing.json').write_text(json.dumps(timings,indent=2))
# Phrase-aligned transcript until the separate ASR word-alignment pass.
(ROOT/'transcript.json').write_text(json.dumps({'alignment':'measured per-scene audio; word alignment pending',
    'segments':[{'text':item['text'],'start':item['speech_start'],'end':item['speech_end']} for item in timings]},indent=2))
print('Narration mix: exactly 120 seconds.',flush=True)
