#!/bin/sh
# Run the full test suite inside this export tree: the server
# suite under server/ and the gateway suite under gateway/tests/,
# each in its own process (both servers are named server.py).
set -e
cd "$(dirname "$0")"
python3 -m unittest discover -s server
python3 -m unittest discover -s gateway/tests
