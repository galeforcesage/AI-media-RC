#!/usr/bin/env python3
import sys, os, datetime
sys.path.insert(0, os.path.expanduser('~/AI-media-RC/backend/orchestrator/src'))
from orchestrator import Orchestrator

class FakeOrch:
    pass

for attr in dir(Orchestrator):
    if attr.startswith('_') and not attr.startswith('__'):
        try:
            setattr(FakeOrch, attr, getattr(Orchestrator, attr))
        except Exception:
            pass

orch = FakeOrch()

tests = [
    'What recorded yesterday?',
    'What is on tonight?',
    "What's recording tomorrow?",
    'What is recording this Sunday?',
    'What recorded last Monday?',
    "What's on next Friday?",
    'What is on Sunday?',
    'What recorded Tuesday?',
    'What recorded last week?',
    "What's recording this week?",
    "What's on next week?",
    'Show me the last 5 days',
    'What recorded on April 20, 2026?',
    "What's on 04/28/2026?",
    'Show recordings for 2026-04-30',
    'What recorded 3 days ago?',
    "What's on in 2 days?",
    'What was on the day before yesterday?',
    'Turn on the TV',
    'What channel is ESPN?',
]

print(f"Today: {datetime.datetime.now().strftime('%Y-%m-%d (%A)')}")
print('=' * 80)
for t in tests:
    result = orch._resolve_dates(t)
    if result != t:
        print(f'  IN:  {t}')
        print(f'  OUT: {result}')
    else:
        print(f'  IN:  {t}')
        print(f'  OUT: (unchanged)')
    print()
