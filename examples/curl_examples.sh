#!/usr/bin/env bash
# 场馆疏散通道编排服务 —— curl 调用示例
# 用法： bash examples/curl_examples.sh [BASE_URL]
# 依次演示：初始化、新增组、新增封闭、推进分钟、409 拒绝、容量管制登记/
# 重叠/超载拒绝、有效上限核验、组纠正、现场节点滞留报告（受理/拒绝/待救援）、
# 人工救援队登记/派遣拒绝/派遣受理/结案核销、修订追溯。
set -euo pipefail

BASE="${1:-${BASE:-http://localhost:8000}}"

say() { printf '\n==== %s ====\n' "$1"; }

say "1) 健康检查"
curl -s "$BASE/health" | python3 -m json.tool

say "2) 全量初始化一个自定义场景（通道/出口/封闭/人群组）"
curl -s -X POST "$BASE/scenario" \
  -H 'Content-Type: application/json' \
  -d '{
    "passages": [
      {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 30},
      {"id": 2, "src": 2, "dst": 3, "minutes": 3, "capacity": 30},
      {"id": 3, "src": 3, "dst": 5, "minutes": 2, "capacity": 30},
      {"id": 5, "src": 2, "dst": 6, "minutes": 2, "capacity": 40},
      {"id": 7, "src": 7, "dst": 5, "minutes": 5, "capacity": 20}
    ],
    "exits": [5, 6],
    "closures": [
      {"edge_id": 1, "start_minute": 3, "end_minute": 10}
    ],
    "groups": [
      {"id": 1, "size": 30, "origin": 1, "earliest_departure": 0, "priority": 10, "deadline": 30},
      {"id": 3, "size": 10, "origin": 2, "earliest_departure": 0, "priority": 20, "deadline": 20},
      {"id": 4, "size": 15, "origin": 7, "earliest_departure": 0, "priority": 5, "deadline": 15}
    ]
  }' | python3 -m json.tool

say "3) 查询编排结果（按优先级、最早出发、组号排序）"
curl -s "$BASE/plan" | python3 -m json.tool

say "4) 新增人群组（仅重排未出发组）"
curl -s -X POST "$BASE/groups" \
  -H 'Content-Type: application/json' \
  -d '[
    {"id": 7, "size": 25, "origin": 1, "earliest_departure": 2, "priority": 8, "deadline": 40}
  ]' | python3 -m json.tool

say "5) 新增不触及已出发组的封闭时段（登记后重排未出发组）"
curl -s -X POST "$BASE/closures" \
  -H 'Content-Type: application/json' \
  -d '{"edge_id": 2, "start_minute": 20, "end_minute": 30}' \
  | python3 -m json.tool

say "6) 推进当前分钟 1 分钟（t=0 出发的组固定，随后重排未出发组）"
curl -s -X POST "$BASE/tick" \
  -H 'Content-Type: application/json' \
  -d '{"minutes": 1}' | python3 -m json.tool

say "7) 尝试封闭已出发组剩余路线（组3正占用通道5的[1,2)，预期 HTTP 409 整次拒绝）"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/closures" \
  -H 'Content-Type: application/json' \
  -d '{"edge_id": 5, "start_minute": 1, "end_minute": 3}'
python3 -m json.tool /tmp/resp.json || true

say "8) 拒绝后再次查询，原编排保持不变"
curl -s "$BASE/plan" | python3 -m json.tool

say "9) 查询通道 1 每分钟容量占用（核验不超容量）"
curl -s "$BASE/passages/1/load" | python3 -m json.tool

say "10) 登记使已出发组超载的容量管制（组3剩余占用通道5的[1,2)共10人，上限5 -> 预期 HTTP 409）"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/capacity-controls" \
  -H 'Content-Type: application/json' \
  -d '{"edge_id": 5, "start_minute": 1, "end_minute": 3, "capacity": 5}'
python3 -m json.tool /tmp/resp.json || true

say "11) 登记合法容量管制（通道1第10..14分钟限流至10人/分钟，登记后重排未出发组）"
curl -s -X POST "$BASE/capacity-controls" \
  -H 'Content-Type: application/json' \
  -d '{"edge_id": 1, "start_minute": 10, "end_minute": 14, "capacity": 10}' \
  | python3 -m json.tool

say "12) 查询通道 1 占用：管制时段内每分钟有效上限收紧为 10，时段外仍为 30"
curl -s "$BASE/passages/1/load" | python3 -m json.tool

say "13) 列出当前全部容量管制时段"
curl -s "$BASE/capacity-controls" | python3 -m json.tool

say "14) 登记与已有管制重叠的时段（通道1第12..16分钟，预期 HTTP 409 整次拒绝）"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/capacity-controls" \
  -H 'Content-Type: application/json' \
  -d '{"edge_id": 1, "start_minute": 12, "end_minute": 16, "capacity": 20}'
python3 -m json.tool /tmp/resp.json || true

say "15) 纠正未出发组（组7：人数 25->20、优先级 8->12，整批原子生效并重排未出发组）"
curl -s -X POST "$BASE/group-corrections" \
  -H 'Content-Type: application/json' \
  -d '[{"group_id": 7, "size": 20, "priority": 12}]' | python3 -m json.tool

say "16) 纠正已出发的组 3（预期 HTTP 409 整批拒绝，状态与修订历史不变）"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/group-corrections" \
  -H 'Content-Type: application/json' \
  -d '[{"group_id": 3, "size": 5}]'
python3 -m json.tool /tmp/resp.json || true

say "17) 含无效项的纠正批（组999不存在，预期 HTTP 422，整批拒绝）"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/group-corrections" \
  -H 'Content-Type: application/json' \
  -d '[{"group_id": 7, "size": 18}, {"group_id": 999, "size": 1}]'
python3 -m json.tool /tmp/resp.json || true

say "18) 重置为便于演示节点滞留报告的小场景，并推进到组1抵达节点2（第4分钟）"
curl -s -X POST "$BASE/scenario" \
  -H 'Content-Type: application/json' \
  -d '{
    "passages": [
      {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
      {"id": 2, "src": 2, "dst": 6, "minutes": 2, "capacity": 10},
      {"id": 3, "src": 2, "dst": 3, "minutes": 2, "capacity": 10},
      {"id": 4, "src": 3, "dst": 5, "minutes": 2, "capacity": 10}
    ],
    "exits": [5, 6],
    "closures": [],
    "groups": [
      {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0, "priority": 10, "deadline": 30},
      {"id": 2, "size": 10, "origin": 1, "earliest_departure": 0, "priority": 5, "deadline": 30},
      {"id": 3, "size": 10, "origin": 2, "earliest_departure": 0, "priority": 1, "deadline": 30}
    ]
  }' > /dev/null
curl -s -X POST "$BASE/tick" -H 'Content-Type: application/json' \
  -d '{"minutes": 4}' | python3 -m json.tool

say "19) 报告未出发的组 2（节点1，预期 HTTP 409 NOT_DEPARTED，不改编排）"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/groups/2/delay-report" \
  -H 'Content-Type: application/json' \
  -d '{"node_id": 1, "recovery_minute": 4}'
python3 -m json.tool /tmp/resp.json || true

say "20) 组 1 报告错误节点（实际在节点2，预期 HTTP 409 NODE_MISMATCH）"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/groups/1/delay-report" \
  -H 'Content-Type: application/json' \
  -d '{"node_id": 1, "recovery_minute": 4}'
python3 -m json.tool /tmp/resp.json || true

say "21) 恢复分钟早于当前分钟（预期 HTTP 422）"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/groups/1/delay-report" \
  -H 'Content-Type: application/json' \
  -d '{"node_id": 2, "recovery_minute": 3}'
python3 -m json.tool /tmp/resp.json || true

say "22) 正式报告：组1在节点2滞留、预计第7分钟恢复（释放旧预约并重找余程，组2随之重排）"
curl -s -X POST "$BASE/groups/1/delay-report" \
  -H 'Content-Type: application/json' \
  -d '{"node_id": 2, "recovery_minute": 7}' | python3 -m json.tool

say "23) 等价的集合式写法：POST /delay-reports（组号置于请求体；此处演示对组2的409拒绝）"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/delay-reports" \
  -H 'Content-Type: application/json' \
  -d '{"group_id": 2, "node_id": 1, "recovery_minute": 4}'
python3 -m json.tool /tmp/resp.json || true

say "24) 待救援场景：唯一余程通道自第5分钟封闭，组1报告恢复6 -> awaiting_rescue"
curl -s -X POST "$BASE/scenario" \
  -H 'Content-Type: application/json' \
  -d '{
    "passages": [
      {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
      {"id": 2, "src": 2, "dst": 6, "minutes": 1, "capacity": 10}
    ],
    "exits": [6],
    "closures": [{"edge_id": 2, "start_minute": 5, "end_minute": 30}],
    "groups": [
      {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0, "priority": 10, "deadline": 30}
    ]
  }' > /dev/null
curl -s -X POST "$BASE/tick" -H 'Content-Type: application/json' \
  -d '{"minutes": 4}' > /dev/null
curl -s -X POST "$BASE/groups/1/delay-report" \
  -H 'Content-Type: application/json' \
  -d '{"node_id": 2, "recovery_minute": 6}' | python3 -m json.tool

say "25) 时间推进不自动重排待救援组（仍为 awaiting_rescue，只保留已走前缀）"
curl -s -X POST "$BASE/tick" -H 'Content-Type: application/json' \
  -d '{"minutes": 6}' | python3 -m json.tool

# ---- 人工救援队：登记 / 派遣 / 结案核销 ----
say "26) 登记人工救援队（队1载员10；重复编号预期 HTTP 409 整次拒绝）"
curl -s -X POST "$BASE/rescue-teams" -H 'Content-Type: application/json' \
  -d '{"team_id": 1, "capacity": 10}' | python3 -m json.tool
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/rescue-teams" \
  -H 'Content-Type: application/json' \
  -d '{"team_id": 1, "capacity": 20}'
python3 -m json.tool /tmp/resp.json || true
curl -s -X POST "$BASE/rescue-teams" -H 'Content-Type: application/json' \
  -d '{"team_id": 2, "capacity": 5}' > /dev/null

say "27) 列出救援队（两队均空闲，task_id 为 null）"
curl -s "$BASE/rescue-teams" | python3 -m json.tool

say "28) 派遣校验：节点不符 409 NODE_MISMATCH（组1实际滞留节点2）"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/rescue-tasks" \
  -H 'Content-Type: application/json' \
  -d '{"group_id": 1, "team_id": 1, "node_id": 1, "observed_minute": 10}'
python3 -m json.tool /tmp/resp.json || true

say "29) 派遣校验：队2载员5小于组人数10 -> 409 TEAM_CAPACITY_EXCEEDED"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/rescue-tasks" \
  -H 'Content-Type: application/json' \
  -d '{"group_id": 1, "team_id": 2, "node_id": 2, "observed_minute": 10}'
python3 -m json.tool /tmp/resp.json || true

say "30) 派遣校验：所见分钟10与当前分钟一致；再演示失步（observed 9 -> 409）"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/rescue-tasks" \
  -H 'Content-Type: application/json' \
  -d '{"group_id": 1, "team_id": 1, "node_id": 2, "observed_minute": 9}'
python3 -m json.tool /tmp/resp.json || true

say "31) 正式派遣：组1在节点2交给队1（当前分钟10），返回任务号；组置 rescuing"
curl -s -X POST "$BASE/rescue-tasks" -H 'Content-Type: application/json' \
  -d '{"group_id": 1, "team_id": 1, "node_id": 2, "observed_minute": 10}' \
  | python3 -m json.tool

say "32) 同组重复派遣 / 队伍忙：组1再派（即使换队2）-> 409 GROUP_RESCUE_IN_PROGRESS"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/rescue-tasks" \
  -H 'Content-Type: application/json' \
  -d '{"group_id": 1, "team_id": 2, "node_id": 2, "observed_minute": 10}'
python3 -m json.tool /tmp/resp.json || true

say "33) 救援中的组拒绝既有滞留报告（409 GROUP_RESCUING）"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/groups/1/delay-report" \
  -H 'Content-Type: application/json' \
  -d '{"node_id": 2, "recovery_minute": 12}'
python3 -m json.tool /tmp/resp.json || true

say "34) 查询任务1（in_progress）与任务列表（?status=in_progress 过滤）"
curl -s "$BASE/rescue-tasks/1" | python3 -m json.tool
curl -s "$BASE/rescue-tasks?status=in_progress" | python3 -m json.tool

say "35) 结案时序：送达12晚于当前10 -> 409 DELIVERY_IN_FUTURE；非既有出口7 -> 422"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/rescue-tasks/1/completion" \
  -H 'Content-Type: application/json' \
  -d '{"exit_id": 6, "delivery_minute": 12}'
python3 -m json.tool /tmp/resp.json || true
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/rescue-tasks/1/completion" \
  -H 'Content-Type: application/json' \
  -d '{"exit_id": 7, "delivery_minute": 10}'
python3 -m json.tool /tmp/resp.json || true

say "36) 推进到第12分钟，结案核销：出口6、实际送达12（晚于原截止也如实记录）"
curl -s -X POST "$BASE/tick" -H 'Content-Type: application/json' \
  -d '{"minutes": 2}' > /dev/null
curl -s -X POST "$BASE/rescue-tasks/1/completion" -H 'Content-Type: application/json' \
  -d '{"exit_id": 6, "delivery_minute": 12}' | python3 -m json.tool

say "37) 结案后：组为 rescued（实际出口/时刻单列，规划到达字段为空），队1空闲"
curl -s "$BASE/plan" | python3 -m json.tool
curl -s "$BASE/rescue-teams" | python3 -m json.tool

say "38) 重复结案 -> 409 TASK_ALREADY_COMPLETED；已救援组再派 -> 409"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/rescue-tasks/1/completion" \
  -H 'Content-Type: application/json' \
  -d '{"exit_id": 6, "delivery_minute": 12}'
python3 -m json.tool /tmp/resp.json || true

say "39) 列出修订历史（派遣与结案各一条 rescue-* 修订，被拒绝的不产生）"
curl -s "$BASE/revisions" | python3 -m json.tool

say "40) 按修订号查询：取最新一条（rescue-completions）的快照与逐组差异"
LATEST=$(curl -s "$BASE/revisions" | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["revisions"][-1]["rev"])')
curl -s "$BASE/revisions/$LATEST" | python3 -m json.tool

say "41) 查询未知修订号与未知任务号（预期 HTTP 404）"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' "$BASE/revisions/999999"
python3 -m json.tool /tmp/resp.json || true
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' "$BASE/rescue-tasks/999999"
python3 -m json.tool /tmp/resp.json || true

say "完成。交互文档： $BASE/docs"
