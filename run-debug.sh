#!/bin/bash
# Launch the app and follow its log.
cd "$(dirname "$0")"
/usr/bin/open -n "Thrivbe Voice.app" "$@"
exec tail -f agent.log
