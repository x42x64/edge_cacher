#!/bin/bash
exec 1>&2
# next line sends SIGTERM to any process accessing the mounted filesystem:
fuser -Mk -SIGTERM -m "$@"
while :;
do
    if fuser -m "$@";
    then 
        echo "Mount $@ is busy, waiting..."; sleep 1
    else 
        fusermount -u "$@"; exit 0
    fi
done
