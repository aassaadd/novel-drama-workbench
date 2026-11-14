#!/bin/bash
# 本地全部参与对标视图的控件采集（只读）
# 用法：bash tools/parity/run_local_all.sh
set -e
cd "$(dirname "$0")/../.."
P='%2Ftmp%2Fndw_parity'
C='chp_01M78DRDHS6X7PKWGDZ8W4G7A0'
PY=.venv/bin/python
$PY tools/parity/collect_local.py --view V01 --name home --hash '#/projects'
$PY tools/parity/collect_local.py --view V02 --name script --hash "#/p/$P/c/$C"
$PY tools/parity/collect_local.py --view V03 --name record --hash "#/p/$P/c/$C" --click '[data-view="record"]' --wait-sel '.rc-row, .rc-empty'
$PY tools/parity/collect_local.py --view V04 --name post --hash "#/p/$P/c/$C" --click '[data-view="post"]' --wait-sel '[data-vact="post-build"]'
$PY tools/parity/collect_local.py --view V05 --name align --hash "#/p/$P/c/$C" --click '[data-act="more-views"]' --click2 '[data-menu-key="align"]'
$PY tools/parity/collect_local.py --view V07 --name characters --hash "#/p/$P/characters"
$PY tools/parity/collect_local.py --view V09 --name media --hash "#/p/$P/media"
$PY tools/parity/collect_local.py --view V10 --name tasks --hash "#/p/$P/tasks"
$PY tools/parity/collect_local.py --view V11 --name settings --hash '#/settings'
$PY tools/parity/collect_local.py --view V11 --name settings-project --hash "#/settings?project=$P"
