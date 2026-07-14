#!/bin/sh
set -eu

if [ -f /config/gateway/sub2-usage.env ]; then
    set -a
    . /config/gateway/sub2-usage.env
    set +a
fi

export PYTHONPATH="/config/gateway/src:/config/gateway/vendor${PYTHONPATH:+:$PYTHONPATH}"
exec python /config/gateway/supervisor.py
