"""Regenerate the demo voice using the configured Azure Speech resource.

Run with the repository venv. Requests are billable. Speech chunks are cached
locally, keys are never printed or stored, and the old narration is retained.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import wave
from xml.sax.saxutils import escape

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1]))
from matchos.llm_config import load_env_files
import os


def duration(path):
    with wave.open(str(path)) as wav:
        return wav.getnframes() / wav.getframerate()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--voices-only',action='store_true')
    args=parser.parse_args()
    load_env_files(ROOT.parents[1])
    key,region=os.getenv('AZURE_SPEECH_KEY'),os.getenv('AZURE_SPEECH_REGION')
    if not key or not region: raise SystemExit('Configure AZURE_SPEECH_KEY and AZURE_SPEECH_REGION locally.')
    voice=os.getenv('VIDEO_NARRATOR_VOICE','en-GB-Emily:MAI-Voice-2.1')
    if not re.fullmatch(r'[A-Za-z0-9:._-]+',voice):raise SystemExit('Invalid narrator voice ID')
    plan=json.loads((ROOT/'narration-plan.json').read_text())
    cache=ROOT/'.tts-cache';cache.mkdir(exist_ok=True)
    timings=[]
    with httpx.Client(timeout=90) as client:
        for part in plan:
            available=part['scene_end']-part['scene_start']-.9
            spoken=escape(part['text']).replace('MatchOS','<sub alias="Match oh ess">MatchOS</sub>').replace('GPT six Luna','<sub alias="G P T six Luna">GPT six Luna</sub>')
            rate=0
            for attempt in range(2):
                ssml=(f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
                      f'xmlns:mstts="http://www.w3.org/2001/mstts" xml:lang="en-GB"><voice name="{voice}">'
                      f'<mstts:express-as style="narrator"><prosody rate="+{rate}%">{spoken}</prosody>'
                      '</mstts:express-as></voice></speak>')
                path=cache/(hashlib.sha256(ssml.encode()).hexdigest()+'.wav')
                if not path.exists():
                    response=client.post(f'https://{region}.tts.speech.microsoft.com/cognitiveservices/v1',content=ssml.encode(),headers={
                        'Ocp-Apim-Subscription-Key':key,'Content-Type':'application/ssml+xml',
                        'X-Microsoft-OutputFormat':'riff-24khz-16bit-mono-pcm'})
                    if response.status_code!=200:raise SystemExit(f'Azure narration failed: HTTP {response.status_code}, section {part["scene"]}')
                    if not response.content.startswith(b'RIFF'):raise SystemExit('Expected PCM WAV response')
                    path.write_bytes(response.content)
                seconds=duration(path)
                if seconds<=available:break
                rate=min(12,round((seconds/available-1)*100)+3)
            if seconds>available:raise SystemExit(f'Shorten section {part["scene"]}: {seconds:.2f}s exceeds {available:.2f}s. Audio was not truncated.')
            item={**part,'speech_start':part['scene_start']+.45,'speech_end':round(part['scene_start']+.45+seconds,3),
                  'voice':voice,'provider':'azure-speech','style':'narrator','rate_percent':rate,'cache_path':str(path.relative_to(ROOT))}
            timings.append(item)
            print(f'Section {part["scene"]}: {seconds:.2f}s, SSML rate +{rate}%',flush=True)
    (ROOT/'timing.json').write_text(json.dumps(timings,indent=2))
    if args.voices_only:return
    cmd=['ffmpeg','-hide_banner','-loglevel','error','-y']
    for item in timings:cmd+=['-i',str(ROOT/item['cache_path'])]
    filters=[]
    for i,item in enumerate(timings):
        delay=round(item['speech_start']*1000)
        filters.append(f'[{i}:a]adelay={delay}:all=1[a{i}]')
    inputs=''.join(f'[a{i}]' for i in range(len(timings)))
    filters.append(inputs+f'amix=inputs={len(timings)}:normalize=0,apad,atrim=duration=120[mix]')
    raw=ROOT/'.tts-cache/mix.wav'
    subprocess.run(cmd+['-filter_complex',';'.join(filters),'-map','[mix]','-ar','24000','-ac','1',str(raw)],check=True)
    scan=subprocess.run(['ffmpeg','-hide_banner','-i',str(raw),'-af','loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json','-f','null','-'],capture_output=True,text=True,check=True)
    levels=json.loads(re.search(r'\{[^{}]+\}',scan.stderr,re.S).group())
    filt=('loudnorm=I=-16:TP=-1.5:LRA=11:linear=false:'
          f'measured_I={levels["input_i"]}:measured_TP={levels["input_tp"]}:measured_LRA={levels["input_lra"]}:'
          f'measured_thresh={levels["input_thresh"]}:offset={levels["target_offset"]}')
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-i',str(raw),'-af',filt,'-ar','24000','-ac','1','-t','120',str(ROOT/'narration.wav')],check=True)
    assert duration(ROOT/'narration.wav')==120
    print('Microsoft narration mastered: exactly 120 seconds.',flush=True)


if __name__=='__main__':main()
