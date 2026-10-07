"""Cold team lookup must always see a complete persisted binding."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os

from jiuwenswarm.server.runtime.session import session_metadata as metadata


def test_reader_keeps_binding_while_metadata_writer_is_pending(tmp_path, monkeypatch):
    session = tmp_path / 'probe'
    session.mkdir()
    target = session / 'metadata.json'
    previous = {'session_id': 'probe', 'team_name': 'coach-event-probe', 'message_count': 0}
    target.write_text(json.dumps(previous))
    monkeypatch.setattr(metadata, 'get_agent_sessions_dir', lambda: tmp_path)
    pending, release = threading.Event(), threading.Event()
    replace = os.replace

    def pause_before_replace(source, destination):
        if Path(destination) == target:
            pending.set()
            assert release.wait(5)
        return replace(source, destination)

    monkeypatch.setattr(os, 'replace', pause_before_replace)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(metadata._write_metadata_sync, 'probe', {**previous, 'message_count': 1})
        try:
            assert pending.wait(5)
            # External processes cannot use the writer's in-process file lock.
            assert json.loads(target.read_text()) == previous
        finally:
            release.set()
        future.result()
    assert metadata._read_metadata('probe', cache_bust=True)['message_count'] == 1
    assert list(session.iterdir()) == [target]
