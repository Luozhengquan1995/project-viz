#!/usr/bin/env python3
"""Portable entry point; everything it imports ships inside this skill."""
import json
import sys

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, 'reconfigure'):
        stream.reconfigure(encoding='utf-8')

if sys.version_info < (3, 10):
    print(json.dumps({'error': 'Project Viz requires Python 3.10 or newer'}), file=sys.stderr)
    raise SystemExit(2)

from project_viz_runtime.cli import main

if __name__ == '__main__':
    try:
        result = main()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as error:
        print(json.dumps({'error': str(error), 'type': type(error).__name__}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
