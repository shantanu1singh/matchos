"""Check HTTP(S), versioned assets, and the WebSocket handshake.

Uses the real application protocol; no model work unless --judge-demo is supplied.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path

import httpx
import websockets
from dotenv import dotenv_values


async def smoke(url, judge_demo=False, speech=False, credentials=None):
    cookie_headers = {}
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(f'{url}/api/health')
        response.raise_for_status()
        assert response.json()['status'] == 'ok'
        if credentials:
            assert (await client.get(url)).status_code == 401, 'Expected an unauthenticated password challenge'
            assert (await client.get(f'{url}/api/speech/missing')).status_code == 401
            try:
                async with websockets.connect(url.replace('https://', 'wss://').replace('http://', 'ws://') + '/ws'):
                    raise AssertionError('Unauthenticated WebSocket was accepted')
            except websockets.exceptions.InvalidStatus as exc:
                assert exc.response.status_code in {401, 403}
        response = await client.get(url, auth=credentials)
        response.raise_for_status()
        assert '/assets/app.js?v=' in response.text
        if credentials:
            cookie_headers = {'Cookie': f'matchos_access={client.cookies["matchos_access"]}'}
        for asset in ('app.js', 'audio.js', 'styles.css'):
            response = await client.get(f'{url}/assets/{asset}')
            response.raise_for_status()
    ws_url = url.replace('https://', 'wss://').replace('http://', 'ws://') + '/ws'
    async with websockets.connect(ws_url, open_timeout=30, additional_headers=cookie_headers, origin=url) as socket:
        hello = json.loads(await asyncio.wait_for(socket.recv(), 30))
        assert hello['type'] == 'hello' and hello['scenarios']
        if judge_demo:
            await socket.send(json.dumps({'type': 'demo', 'step': 1, 'requestId': 'deployment-smoke'}))
            checkpoint_ms, frames, updates = None, [], []
            playing = False
            async with asyncio.timeout(120):
                while True:
                    message = json.loads(await socket.recv())
                    if message['type'] == 'demo_snapshot':
                        checkpoint_ms = message['frame']['timestamp_ms']
                    elif message['type'] == 'frame':
                        frames.append(message['frame']['timestamp_ms'])
                    elif message['type'] == 'viewer_update':
                        updates.append((message['index'], len(frames)))
                    elif message['type'] == 'demo_status' and message['phase'] == 'playing':
                        playing = message.get('running') is True
                    if message['type'] == 'demo_status' and message['phase'] in {'ready', 'error'}:
                        assert message['phase'] == 'ready', 'Judge demo failed'
                        assert playing and checkpoint_ms is not None and frames, 'Judge demo did not play live frames'
                        assert frames[-1] - checkpoint_ms >= 12_000, 'Judge demo clip ended too early'
                        assert all(after > before for before, after in zip(frames, frames[1:])), 'Playback time did not advance'
                        assert {index for index, _ in updates} == {0, 1, 2}, 'Viewer updates did not arrive beside playback'
                        assert all(count < len(frames) for _, count in updates), 'Playback stopped before viewer results arrived'
                        assert message.get('running') is False, 'Judge demo did not pause after playback'
                        results = message['results']
                        assert len(results) == 3
                        for item in results:
                            result = item['result']
                            assert result['decision']['provider'] == 'azure-foundry:Decision-1', 'Decision provider fell back'
                            assert result['producer']['action'] == 'AIR', 'Goal did not air'
                            assert result['overlay']['provider'] == 'azure-foundry:gpt-6-luna', 'Reasoning provider fell back'
                        print('PASS: goal checkpoint settled all three personas using Azure Decision-1 and gpt-6-luna.')
                        print(f'PASS: live playback advanced {(frames[-1] - checkpoint_ms) / 1000:.1f}s across {len(frames)} frames, then paused after viewer updates.')
                        if speech:
                            available = [item['speech'] for item in results if (item.get('speech') or {}).get('id')]
                            assert available, 'No speech registered for aired stories'
                            async with httpx.AsyncClient(timeout=30) as client:
                                audio = await client.get(f'{url}/api/speech/{available[0]["id"]}', headers=cookie_headers)
                                audio.raise_for_status()
                                assert audio.headers['content-type'].startswith('audio/mpeg') and len(audio.content) > 100
                            print('PASS: Azure Speech synthesis returned audio.')
                        break
    print('PASS: health, page, assets, and WebSocket hello.' + ('' if judge_demo else ' No paid model calls made.'))
    if credentials:
        print('PASS: unauthenticated page/audio/socket blocked; authenticated cookie supports assets, socket and audio.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url')
    parser.add_argument('--judge-demo', action='store_true', help='Runs the real three-persona goal checkpoint; incurs inference charges.')
    parser.add_argument('--speech', action='store_true', help='Also synthesize one aired story; incurs Speech charges. Requires --judge-demo.')
    parser.add_argument('--auth-env-file', type=Path, help='Read basic auth username/password from an ignored dotenv file, never command-line values.')
    args = parser.parse_args()
    if args.speech and not args.judge_demo:
        parser.error('--speech requires --judge-demo')
    config = {**(dotenv_values(args.auth_env_file) if args.auth_env_file else {}), **os.environ}
    credentials = None
    if config.get('MATCHOS_BASIC_AUTH_PASSWORD'):
        credentials = (config.get('MATCHOS_BASIC_AUTH_USERNAME', 'judges'), config['MATCHOS_BASIC_AUTH_PASSWORD'])
    asyncio.run(smoke(args.url.rstrip('/'), args.judge_demo, args.speech, credentials))
