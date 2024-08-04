#!/bin/sh
if [ -e ./secrets ]; then
  source ./secrets
  export TOKEN=$TOKEN
fi
python scripts/versions.py --path=recipes --commit
