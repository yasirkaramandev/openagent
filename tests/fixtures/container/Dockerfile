FROM alpine:3.20
RUN printf '%s\n' \
    '#!/bin/sh' \
    'set -eu' \
    'test "$(ls /sys/class/net)" = lo' \
    "grep -q ' /workspace .*tmpfs' /proc/mounts" \
    'test "$(cat input.txt)" = "host snapshot"' \
    "printf 'container result\\n' > result.txt" \
    > /usr/local/bin/pytest && chmod 0755 /usr/local/bin/pytest
CMD ["/bin/sh"]
