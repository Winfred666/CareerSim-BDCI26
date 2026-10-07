"""Resume interrupted transport requests without replacing accepted answers."""
import argparse
import asyncio
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import time

from dotenv import dotenv_values
from openai import AsyncOpenAI

from scripts.benchmark_bare_correction import OUTPUT, ROOT, canonical, read, request_for, save


async def recover(root):
    lock = (OUTPUT / '.benchmark.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    manifest = read(root / 'manifest.json')
    reference = root.parent / manifest['reference']
    original = read(reference / 'manifest.json')
    env = dotenv_values(ROOT / '.env')
    if (env.get('MODEL_NAME') != manifest['model'] or
            hashlib.sha256((env.get('API_BASE') or '').encode()).hexdigest() != original['api']['base_sha256']):
        raise ValueError('Approved provider/model changed')
    phases_path = root / 'controller-phases.json'
    phases = read(phases_path) if phases_path.exists() else []
    phases.append(dict(stopped_utc=datetime.now(timezone.utc).isoformat(),
                       summary=read(root / 'summary.json'), reason='Transport failure recovery'))
    save(phases_path, phases)
    candidates = []
    for path in (reference / 'events').glob('*.json'):
        record = read(path)
        if record.get('accepted'):
            continue
        attempts = record.get('attempts', [])
        if len(attempts) + bool(record.get('in_flight')) < 2:
            continue
        if any(a.get('error') not in {'APIConnectionError', 'APITimeoutError', 'InterruptedUnknownUsage'}
               for a in attempts):
            raise ValueError(f'Non-transport failure requires inspection: {path.stem}')
        identity = {k: record[k] for k in ('version', 'request', 'public_cache_context')}
        packet = json.loads(record['request']['messages'][1]['content'])
        if (hashlib.sha256(canonical(identity).encode()).hexdigest() != path.stem or
                record['request'] != request_for(packet, manifest['system'], manifest['model'])):
            raise ValueError('Stored request identity changed')
        candidates.append((path, record, packet))
    print(json.dumps(dict(recovery_candidates=len(candidates))), flush=True)
    semaphore = asyncio.Semaphore(6)
    async with AsyncOpenAI(api_key=env['API_KEY'], base_url=env['API_BASE'], timeout=90, max_retries=0) as client:
        async def one(path, record, packet):
            async with semaphore:
                if record.get('in_flight'):
                    record['attempts'].append(dict(record.pop('in_flight'), error='InterruptedUnknownUsage', usage=None))
                    save(path, record)
                item = dict(attempt=len(record['attempts']), started_utc=datetime.now(timezone.utc).isoformat(),
                            usage=None, recovery='Same stored request after transport interruption')
                record['in_flight'] = item
                save(path, record)
                start = time.monotonic()
                try:
                    response = await client.chat.completions.create(**record['request'])
                    item['response'] = response.choices[0].message.model_dump(exclude_none=True)
                    if response.usage is not None:
                        item['usage'] = response.usage.model_dump(exclude_none=True)
                    calls = response.choices[0].message.tool_calls
                    if not calls or len(calls) != 1 or calls[0].function.name != 'take_action':
                        raise ValueError('Expected one take_action tool call')
                    answer = json.loads(calls[0].function.arguments)
                    if (set(answer) != {'choice', 'notes'} or type(answer['choice']) is not int or
                            answer['choice'] not in {o['choice'] for o in packet['options']} or
                            not isinstance(answer['notes'], str) or not answer['notes'].strip()):
                        raise ValueError('Unusable action answer')
                    record.update(accepted=True, answer=answer)
                    item['accepted'] = True
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    item['error'] = type(exc).__name__
                    if getattr(exc, 'status_code', None) is not None:
                        item['status_code'] = exc.status_code
                item['seconds'] = round(time.monotonic() - start, 3)
                record['attempts'].append(item)
                record.pop('in_flight', None)
                save(path, record)
                print(json.dumps(dict(key=path.stem, accepted=bool(record.get('accepted')),
                                      error=item.get('error'), status=item.get('status_code'))), flush=True)
                return bool(record.get('accepted'))
        results = await asyncio.gather(*(one(*c) for c in candidates))
    if not all(results):
        raise RuntimeError('Transport recovery incomplete; original attempts and usage retained')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    asyncio.run(recover(parser.parse_args().root))
