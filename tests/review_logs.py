"""Public log fixtures matching the emulator's action-log interface."""
from pathlib import Path


def write_action_log(path, observation, choice, updates=None, suffix=''):
    path = Path(path)
    current = observation['current_state']
    selected = next(c for c in observation['choices'] if c['choice'] == choice)
    title = observation['current_event']['title']
    month = current['time']['current_month']
    effects = ', '.join(f'{key} {value:+d}' for key, value in (updates or {}).items())
    path.write_text(f'Log file: {path.resolve()}\n'
                    f'[action] Month {month} | {title}\n'
                    f"Chose #{choice}: {selected['action']}\n"
                    + (effects + '\n' if effects else '') + suffix, encoding='utf-8')
    return path
