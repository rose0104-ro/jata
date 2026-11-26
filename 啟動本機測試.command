#!/bin/bash
cd "$(dirname "$0")" || exit 1
export JATA_FORMAL=0
(sleep 1; open 'http://127.0.0.1:8765/') &
python3 server.py --port 8765
