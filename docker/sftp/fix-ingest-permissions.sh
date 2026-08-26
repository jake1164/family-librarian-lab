#!/bin/sh
# atmoz/sftp only chowns a configured home subdirectory when it creates that
# directory itself; a pre-existing Docker-volume mount point at the same path
# (this lab's shared cwa-ingest volume) stays root-owned, so the sftp user
# gets "Permission denied" writing into it. atmoz/sftp's documented
# /etc/sftp.d/ hook runs as root, but before the user database is populated
# (confirmed for real: `chown cwaftp:cwaftp` fails "unknown user/group" here)
# -- so this chowns by the numeric uid:gid instead, which needs no user
# lookup and matches the uid:gid this compose file's user string sets up.
chown -R 1654:1654 /home/cwaftp/upload
