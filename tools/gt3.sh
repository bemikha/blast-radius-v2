#!/bin/bash
# ground truth keeping pytest's whole short summary (subtest descriptions can span lines)
cd /home/claude/br/gt_repo
mkdir -p /home/claude/br/gt3
for p in "$@"; do
  n=$(basename $p .patch); [ -f /home/claude/br/gt3/$n.txt ] && continue
  git apply $p || { echo APPLYFAIL > /home/claude/br/gt3/$n.txt; continue; }
  SKIP_INTEGRATION=0 timeout 600 python -m pytest tests -q -p no:cacheprovider -n 2 -rfE --timeout 180 $DESEL 2>&1 \
    | sed -n '/short test summary info/,$p' > /home/claude/br/gt3/$n.tmp
  mv /home/claude/br/gt3/$n.tmp /home/claude/br/gt3/$n.txt
  git checkout -q -- . ; git clean -qfd sqlglot tests
done
