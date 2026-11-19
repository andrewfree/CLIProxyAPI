#!/bin/sh
out="$1"
for dest in will.local 100.98.74.67; do
  ssh -o BatchMode=yes -o ConnectTimeout=8 "$dest" true 2>"$out.err"; rc=$?
  printf '%s rc=%s err=%s\n' "$dest" "$rc" "$(tr '\n' ' ' < "$out.err" | cut -c1-160)" >> "$out"
done
rm -f "$out.err"
echo done >> "$out"
