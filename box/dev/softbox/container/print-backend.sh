#!/bin/sh
# Print backend of the software box: every job of a queue "softbox:/<queue>" ends,
# byte for byte, in /var/lib/softbox/prints/<queue>-job-<number>.

# Called without arguments, a backend lists the printers it found: this one has none to announce.
[ $# -eq 0 ] && exit 0

output="/var/lib/softbox/prints/${DEVICE_URI#softbox:/}-job-$1"
# The job is written under a name no reader looks for, then renamed: a reader sees the file whole or not at all.
pending="/var/lib/softbox/prints/.${DEVICE_URI#softbox:/}-pending-$1"
umask 022
if [ $# -ge 6 ]; then
    cat "$6" > "$pending"
else
    cat > "$pending"
fi
mv "$pending" "$output"
