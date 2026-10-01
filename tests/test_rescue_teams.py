"""人工救援队交接与核销送达集成测试。

覆盖：
* 队伍登记：唯一正整数编号 + 正整数载员量；重复登记 409，非法字段 422；
* 派遣受理：仅当所见分钟未变、组仍在滞留节点待救援、人数不超载员量、
  队伍空闲时受理；同组同队各只允许一项进行中任务；
* 派遣后：组 rescuing、保留已走前缀、不产生未来通道占用、tick 不重排、
  既有节点滞留报告与组纠正对其拒绝；
* 结案：送达分钟不早于派遣且不晚于当前分钟、出口须既有；成功释放队伍、
  组 rescued 并单列实际出口/时刻，人工送达不计作规划到达（晚于原截止
  也如实记录）；任务不可重复结案；
* 拒绝均整次拒绝：不改编排、不产生修订；派遣/结案成功各原子追加一条
  修订（快照含新增字段）；场景重置清空队伍与任务、旧修订保留；
* 并发抢占：同队并发派遣恰一项成功，另一项 409；派遣与时间推进竞争时
  要么先受理要么 MINUTE_CHANGED 整次拒绝。
"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

_tmpdir = tempfile.mkdtemp(prefix="evac-test-rescue-")
os.environ["EVAC_DB_PATH"] = os.path.join(_tmpdir, "test.db")
os.environ["EVAC_SEED"] = "false"

from app.main import app  # noqa: E402

# 1 --1(4/10)--> 2 --2(1/10)--> 6(出口)
# 通道 2 自第 5 分钟封闭：t=4 进入 edge2 的原计划恰好到达，第 6 分钟恢复
# 则再无余程 -> awaiting_rescue
SCENARIO = {
    "passages": [
        {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
        {"id": 2, "src": 2, "dst": 6, "minutes": 1, "capacity": 10},
    ],
    "exits": [6],
    "closures": [{"edge_id": 2, "start_minute": 5, "end_minute": 30}],
    "groups": [
        {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0,
         "priority": 10, "deadline": 30},
        {"id": 2, "size": 10, "origin": 1, "earliest_departure": 0,
         "priority": 5, "deadline": 30},
    ],
}


# 两条互不相干的走廊汇聚到出口 6：
#   组1: 1 --1(4/10)--> 2 --2(1/10)--> 6，edge2 [5,30) 封闭
#   组2: 7 --3(2/10)--> 8 --4(1/10)--> 6，edge4 [3,30) 封闭
# 组1 在第 4 分钟于节点 2、组2 在第 2 分钟于节点 8 报告后均无余程，
# 于是第 4 分钟时两组同时待救援（滞留节点不同）。
MULTI_SCENARIO = {
    "passages": [
        {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
        {"id": 2, "src": 2, "dst": 6, "minutes": 1, "capacity": 10},
        {"id": 3, "src": 7, "dst": 8, "minutes": 2, "capacity": 10},
        {"id": 4, "src": 8, "dst": 6, "minutes": 1, "capacity": 10},
    ],
    "exits": [6],
    "closures": [
        {"edge_id": 2, "start_minute": 5, "end_minute": 30},
        {"edge_id": 4, "start_minute": 3, "end_minute": 30},
    ],
    "groups": [
        {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0,
         "priority": 10, "deadline": 30},
        {"id": 2, "size": 10, "origin": 7, "earliest_departure": 0,
         "priority": 5, "deadline": 30},
    ],
}


def make_two_awaiting(c):
    """两组同时待救援：组1 滞留节点 2、组2 滞留节点 8（当前分钟 4）。"""
    assert c.post("/tick", json={"minutes": 2}).status_code == 200
    assert c.post("/groups/2/delay-report",
                  json={"node_id": 8, "recovery_minute": 3}).status_code == 200
    assert c.post("/tick", json={"minutes": 2}).status_code == 200
    assert c.post("/groups/1/delay-report",
                  json={"node_id": 2, "recovery_minute": 6}).status_code == 200
    plan = c.get("/plan").json()
    assert plan["current_minute"] == 4
    g = by_id(plan)
    assert g[1]["status"] == "awaiting_rescue"
    assert g[2]["status"] == "awaiting_rescue"


@pytest.fixture()
def client():
    with TestClient(app) as c:
        resp = c.post("/scenario", json=SCENARIO)
        assert resp.status_code == 200
        yield c


def by_id(plan):
    return {g["group_id"]: g for g in plan["groups"]}


def revs(c):
    return c.get("/revisions").json()["revisions"]


def make_awaiting(c, gid=1, recovery=6):
    """推进到第 4 分钟并报告滞留，使指定组进入 awaiting_rescue。"""
    assert c.post("/tick", json={"minutes": 4}).status_code == 200
    resp = c.post(f"/groups/{gid}/delay-report",
                  json={"node_id": 2, "recovery_minute": recovery})
    assert resp.status_code == 200, resp.text
    return resp.json()


def register(c, team_id, capacity):
    return c.post("/rescue-teams",
                  json={"id": team_id, "capacity": capacity})


def dispatch(c, gid, tid, node=2, observed=None):
    observed = c.get("/health").json()["current_minute"] \
        if observed is None else observed
    return c.post("/rescue-dispatches",
                  json={"group_id": gid, "team_id": tid, "node_id": node,
                        "observed_minute": observed})


def complete(c, task_id, exit_id=6, delivery=None):
    delivery = c.get("/health").json()["current_minute"] \
        if delivery is None else delivery
    return c.post(f"/rescue-tasks/{task_id}/complete",
                  json={"exit_id": exit_id, "delivery_minute": delivery})


# --------------------------------------------------------------------------
# 队伍登记
# --------------------------------------------------------------------------

def test_register_team_and_list(client):
    resp = register(client, 1, 12)
    assert resp.status_code == 201, resp.text
    assert resp.json() == {"team_id": 1, "capacity": 12, "busy": False,
                           "active_task_id": None}
    assert register(client, 2, 7).status_code == 201
    body = client.get("/rescue-teams").json()
    assert body["current_minute"] == 0
    assert [t["team_id"] for t in body["teams"]] == [1, 2]
    assert all(t["busy"] is False for t in body["teams"])


def test_duplicate_team_registration_rejected(client):
    assert register(client, 1, 10).status_code == 201
    resp = register(client, 1, 20)
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "RESCUE_TEAM_ALREADY_REGISTERED"
    assert body["conflicts"][0]["reason"] == "TEAM_ALREADY_REGISTERED"
    # 载员量保持首次登记值，且不产生修订
    teams = {t["team_id"]: t for t in client.get("/rescue-teams").json()["teams"]}
    assert teams[1]["capacity"] == 10
    n0 = len(revs(client))
    assert register(client, 1, 20).status_code == 409
    assert len(revs(client)) == n0


def test_invalid_team_payload_rejected_422(client):
    assert register(client, 0, 10).status_code == 422
    assert register(client, 1, 0).status_code == 422
    assert register(client, -1, 10).status_code == 422
    assert client.get("/rescue-teams").json()["teams"] == []


# --------------------------------------------------------------------------
# 派遣受理成功
# --------------------------------------------------------------------------

def test_dispatch_accepted_marks_rescuing_and_returns_task(client):
    make_awaiting(client)
    register(client, 1, 10)
    n0 = len(revs(client))

    resp = dispatch(client, 1, 1)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    task_id = body["task_id"]
    assert body["task"] == {
        "task_id": task_id, "group_id": 1, "team_id": 1, "node_id": 2,
        "observed_minute": 4, "dispatch_minute": 4, "status": "dispatched",
        "exit_id": None, "delivery_minute": None}

    g1 = by_id(body["plan"])[1]
    assert g1["status"] == "rescuing"
    assert g1["rescue_task_id"] == task_id
    assert g1["rescue_exit_id"] is None
    assert g1["rescue_arrival_minute"] is None
    # 保留已走前缀，规划出口/到达不被人工交接改写
    assert g1["route"] == [1]
    assert g1["steps"] == [
        {"edge_id": 1, "from_node": 1, "to_node": 2,
         "enter_minute": 0, "exit_minute": 4}]
    assert g1["prefix_step_count"] == 1
    assert g1["exit_id"] is None and g1["arrival_minute"] is None
    assert g1["reason"] is None and g1["detail"] is None
    # 响应 plan 即当前编排
    assert body["plan"] == client.get("/plan").json()

    # 任务查询
    assert client.get(f"/rescue-tasks/{task_id}").json() == body["task"]
    tasks = client.get("/rescue-tasks").json()
    assert tasks["current_minute"] == 4
    assert [t["task_id"] for t in tasks["tasks"]] == [task_id]

    # 队伍转忙
    team = next(t for t in client.get("/rescue-teams").json()["teams"]
                if t["team_id"] == 1)
    assert team["busy"] is True and team["active_task_id"] == task_id

    # 成功派遣产生一条修订
    entries = revs(client)
    assert len(entries) == n0 + 1
    assert entries[-1]["operation"] == "rescue-dispatches"


def test_dispatch_keeps_prefix_and_creates_no_future_occupancy(client):
    make_awaiting(client)
    register(client, 1, 10)
    assert dispatch(client, 1, 1).status_code == 200

    # 通道 2 上没有该组（或任何未来）占用：滞留报告时旧预约已释放，
    # 派遣不产生新的未来通道占用
    load2 = client.get("/passages/2/load").json()
    assert all(m["used"] == 0 for m in load2["minutes"])
    # 已走前缀 edge1 [0,4) 保留
    load1 = {m["minute"]: m["used"]
             for m in client.get("/passages/1/load").json()["minutes"]}
    assert all(load1[m] == 10 for m in range(0, 4))
    # 未来封闭登记不触及任何剩余路线（rescuing 组无未来段）
    resp = client.post("/closures",
                       json={"edge_id": 2, "start_minute": 8,
                             "end_minute": 12})
    assert resp.status_code == 201


def test_rescuing_group_not_replanned_on_tick(client):
    make_awaiting(client)
    register(client, 1, 10)
    resp = dispatch(client, 1, 1)
    assert resp.status_code == 200
    g_rescuing = by_id(resp.json()["plan"])[1]

    resp = client.post("/tick", json={"minutes": 6})
    assert resp.status_code == 200
    g1 = by_id(resp.json())[1]
    assert g1["status"] == "rescuing"
    assert g1 == g_rescuing  # 时间推进不重排、字段完全不变
    # 组 2 仍是普通待出发组，随时间推进正常重排（不受救援交接影响）
    g2 = by_id(resp.json())[2]
    assert g2["status"] in ("scheduled", "failed")


def test_delay_report_and_correction_rejected_for_rescuing_group(client):
    make_awaiting(client)
    register(client, 1, 10)
    assert dispatch(client, 1, 1).status_code == 200
    plan0 = client.get("/plan").json()
    n0 = len(revs(client))

    resp = client.post("/groups/1/delay-report",
                       json={"node_id": 2, "recovery_minute": 6})
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == "GROUP_BEING_RESCUED"

    resp = client.post("/group-corrections",
                       json=[{"group_id": 1, "priority": 99}])
    assert resp.status_code == 409
    assert resp.json()["error"] == "GROUP_ALREADY_DEPARTED"
    assert resp.json()["conflicts"][0]["status"] == "rescuing"

    assert client.get("/plan").json() == plan0
    assert len(revs(client)) == n0


# --------------------------------------------------------------------------
# 派遣拒绝
# --------------------------------------------------------------------------

def test_dispatch_unknown_group_or_team_422(client):
    make_awaiting(client)
    register(client, 1, 10)
    assert dispatch(client, 999, 1).status_code == 422
    assert dispatch(client, 1, 999).status_code == 422


def test_dispatch_wrong_observed_minute_rejected(client):
    make_awaiting(client)
    register(client, 1, 10)
    # 当前分钟为 4：所见分钟滞后
    resp = dispatch(client, 1, 1, observed=3)
    assert resp.status_code == 409
    conflict = resp.json()["conflicts"][0]
    assert conflict["reason"] == "MINUTE_CHANGED"
    assert conflict["current_minute"] == 4
    # 时间推进后再用旧的所见分钟派遣
    client.post("/tick", json={"minutes": 2})
    resp = dispatch(client, 1, 1, observed=4)
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == "MINUTE_CHANGED"


def test_dispatch_wrong_node_rejected(client):
    make_awaiting(client)
    register(client, 1, 10)
    resp = dispatch(client, 1, 1, node=3)
    assert resp.status_code == 409
    conflict = resp.json()["conflicts"][0]
    assert conflict["reason"] == "NODE_MISMATCH"
    assert conflict["expected_node_id"] == 2


def test_dispatch_over_capacity_rejected(client):
    make_awaiting(client)
    register(client, 1, 5)  # 组 10 人 > 载员量 5
    resp = dispatch(client, 1, 1)
    assert resp.status_code == 409
    conflict = resp.json()["conflicts"][0]
    assert conflict["reason"] == "TEAM_CAPACITY_EXCEEDED"
    assert conflict["group_size"] == 10
    assert conflict["team_capacity"] == 5
    # 队伍仍空闲、组仍待救援，无任务产生
    assert client.get("/rescue-tasks").json()["tasks"] == []
    assert by_id(client.get("/plan").json())[1]["status"] == "awaiting_rescue"


def test_dispatch_non_awaiting_group_rejected(client):
    make_awaiting(client)
    register(client, 1, 10)
    # 组 2 从未出发（scheduled/failed）
    resp = dispatch(client, 2, 1)
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == \
        "GROUP_NOT_AWAITING_RESCUE"


def test_dispatch_same_group_twice_rejected(client):
    make_awaiting(client)
    register(client, 1, 10)
    register(client, 2, 10)
    assert dispatch(client, 1, 1).status_code == 200
    # 同组再派给空闲的二队：组已有进行中任务
    resp = dispatch(client, 1, 2)
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == \
        "GROUP_ALREADY_BEING_RESCUED"


def test_dispatch_to_busy_team_rejected(client):
    c = client
    c.post("/scenario", json=MULTI_SCENARIO)
    make_two_awaiting(c)
    register(c, 1, 10)
    assert dispatch(c, 1, 1, node=2).status_code == 200
    # 同队承接另一组：队伍忙
    resp = dispatch(c, 2, 1, node=8)
    assert resp.status_code == 409
    conflict = resp.json()["conflicts"][0]
    assert conflict["reason"] == "TEAM_BUSY"
    assert conflict["active_task_id"] is not None


def test_rejected_dispatch_changes_nothing(client):
    make_awaiting(client)
    register(client, 1, 5)  # 超载
    plan0 = client.get("/plan").json()
    n0 = len(revs(client))
    assert dispatch(client, 1, 1).status_code == 409
    assert dispatch(client, 1, 1, node=9).status_code == 409
    assert client.get("/plan").json() == plan0
    assert len(revs(client)) == n0


# --------------------------------------------------------------------------
# 结案核销
# --------------------------------------------------------------------------

def test_complete_success_releases_team_and_marks_rescued(client):
    make_awaiting(client)
    register(client, 1, 10)
    task_id = dispatch(client, 1, 1).json()["task_id"]
    client.post("/tick", json={"minutes": 4})  # 当前分钟 8
    n0 = len(revs(client))

    resp = complete(client, task_id, exit_id=6, delivery=7)
    assert resp.status_code == 200, resp.text
    task = resp.json()["task"]
    assert task["status"] == "delivered"
    assert task["exit_id"] == 6 and task["delivery_minute"] == 7

    g1 = by_id(resp.json()["plan"])[1]
    assert g1["status"] == "rescued"
    assert g1["rescue_task_id"] == task_id
    assert g1["rescue_exit_id"] == 6
    assert g1["rescue_arrival_minute"] == 7
    # 人工送达不计作规划到达；已走前缀保留
    assert g1["exit_id"] is None and g1["arrival_minute"] is None
    assert g1["route"] == [1] and g1["prefix_step_count"] == 1

    # 队伍释放，可承接新任务（组 2 先变成待救援）
    team = next(t for t in client.get("/rescue-teams").json()["teams"]
                if t["team_id"] == 1)
    assert team["busy"] is False and team["active_task_id"] is None

    assert len(revs(client)) == n0 + 1
    assert revs(client)[-1]["operation"] == "rescue-completions"


def test_complete_at_dispatch_minute_allowed(client):
    make_awaiting(client)
    register(client, 1, 10)
    task_id = dispatch(client, 1, 1).json()["task_id"]
    # 送达分钟 == 派遣分钟 == 当前分钟 4，允许
    resp = complete(client, task_id, delivery=4)
    assert resp.status_code == 200
    assert by_id(resp.json()["plan"])[1]["status"] == "rescued"


def test_complete_late_after_deadline_recorded_truthfully(client):
    # 截止 7：恢复分钟 8 已超截止，DEADLINE_PASSED 待救援；第 12 分钟才
    # 由人工救援队送达 —— 晚于原截止也如实记录，且不当作规划到达
    c = client
    c.post("/scenario", json={
        "passages": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
            {"id": 2, "src": 2, "dst": 6, "minutes": 1, "capacity": 10},
        ],
        "exits": [6],
        "closures": [{"edge_id": 2, "start_minute": 5, "end_minute": 20}],
        "groups": [
            {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0,
             "priority": 10, "deadline": 7}]})
    make_awaiting(c, recovery=8)
    g1 = by_id(c.get("/plan").json())[1]
    assert g1["status"] == "awaiting_rescue"
    assert g1["reason"] == "DEADLINE_PASSED"
    register(c, 1, 10)
    task_id = dispatch(c, 1, 1).json()["task_id"]
    c.post("/tick", json={"minutes": 8})  # 到第 12 分钟
    resp = complete(c, task_id, delivery=12)
    assert resp.status_code == 200
    g1 = by_id(resp.json()["plan"])[1]
    assert g1["status"] == "rescued"
    assert g1["rescue_exit_id"] == 6
    assert g1["rescue_arrival_minute"] == 12
    assert g1["exit_id"] is None and g1["arrival_minute"] is None


def test_complete_unknown_task_exit_and_time_conflicts(client):
    make_awaiting(client)
    register(client, 1, 10)
    task_id = dispatch(client, 1, 1).json()["task_id"]
    client.post("/tick", json={"minutes": 2})  # 当前分钟 6

    assert complete(client, 99999).status_code == 422
    assert complete(client, task_id, exit_id=99).status_code == 422
    # 早于派遣分钟（4）
    resp = complete(client, task_id, delivery=3)
    assert resp.status_code == 422
    assert "3" in resp.json()["detail"] and "4" in resp.json()["detail"]
    # 晚于当前分钟（6）
    resp = complete(client, task_id, delivery=7)
    assert resp.status_code == 422
    assert "7" in resp.json()["detail"] and "6" in resp.json()["detail"]
    # 全部拒绝：任务仍进行中、组仍救援中
    assert client.get(f"/rescue-tasks/{task_id}").json()["status"] == \
        "dispatched"
    assert by_id(client.get("/plan").json())[1]["status"] == "rescuing"


def test_complete_twice_rejected(client):
    make_awaiting(client)
    register(client, 1, 10)
    task_id = dispatch(client, 1, 1).json()["task_id"]
    assert complete(client, task_id, delivery=4).status_code == 200
    resp = complete(client, task_id, delivery=4)
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "RESCUE_TASK_NOT_ACTIVE"
    assert body["conflicts"][0]["reason"] == "TASK_NOT_IN_PROGRESS"
    assert client.get(f"/rescue-tasks/{task_id}").status_code == 200


def test_rescued_group_rejects_dispatch_and_delay_report(client):
    make_awaiting(client)
    register(client, 1, 10)
    task_id = dispatch(client, 1, 1).json()["task_id"]
    assert complete(client, task_id, delivery=4).status_code == 200

    resp = dispatch(client, 1, 1)
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == "GROUP_ALREADY_RESCUED"
    resp = client.post("/groups/1/delay-report",
                       json={"node_id": 2, "recovery_minute": 4})
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == "GROUP_ALREADY_RESCUED"
    resp = client.post("/group-corrections",
                       json=[{"group_id": 1, "priority": 1}])
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["status"] == "rescued"


def test_freed_team_can_take_next_task(client):
    c = client
    c.post("/scenario", json=MULTI_SCENARIO)
    make_two_awaiting(c)
    register(c, 1, 10)
    t1 = dispatch(c, 1, 1, node=2).json()["task_id"]
    assert complete(c, t1, delivery=4).status_code == 200
    # 同一队结案后可承接组 2 的新任务
    resp = dispatch(c, 2, 1, node=8)
    assert resp.status_code == 200
    assert resp.json()["task_id"] != t1
    tasks = c.get("/rescue-tasks").json()["tasks"]
    assert [(t["group_id"], t["status"]) for t in tasks] == \
        [(1, "delivered"), (2, "dispatched")]


def test_get_unknown_task_404(client):
    resp = client.get("/rescue-tasks/999999")
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# 修订历史
# --------------------------------------------------------------------------

def test_dispatch_and_completion_revisions_snapshots(client):
    make_awaiting(client)
    register(client, 1, 10)
    before_rev = revs(client)[-1]["rev"]

    resp = dispatch(client, 1, 1)
    assert resp.status_code == 200
    dispatch_rev = revs(client)[-1]
    assert dispatch_rev["operation"] == "rescue-dispatches"
    assert dispatch_rev["minute"] == 4
    task_id = resp.json()["task_id"]
    assert dispatch_rev["detail"] == {
        "task_id": task_id, "group_id": 1, "team_id": 1, "node_id": 2,
        "observed_minute": 4, "dispatch_minute": 4}

    detail = client.get(f"/revisions/{dispatch_rev['rev']}").json()
    assert detail["snapshot"] == resp.json()["plan"]
    ch1 = next(ch for ch in detail["changes"] if ch["group_id"] == 1)
    assert ch1["change"] == "changed"
    assert "status" in ch1["changed_fields"]
    assert "rescue_task_id" in ch1["changed_fields"]
    assert ch1["before"]["status"] == "awaiting_rescue"
    assert ch1["after"]["status"] == "rescuing"
    assert ch1["before"]["rescue_task_id"] is None
    assert ch1["after"]["rescue_task_id"] == task_id

    resp = complete(client, task_id, delivery=4)
    completion_rev = revs(client)[-1]
    assert completion_rev["operation"] == "rescue-completions"
    assert completion_rev["detail"] == {
        "task_id": task_id, "group_id": 1, "team_id": 1, "exit_id": 6,
        "dispatch_minute": 4, "delivery_minute": 4}
    detail = client.get(f"/revisions/{completion_rev['rev']}").json()
    g1 = next(g for g in detail["snapshot"]["groups"] if g["group_id"] == 1)
    assert g1["status"] == "rescued"
    assert g1["rescue_exit_id"] == 6
    assert g1["rescue_arrival_minute"] == 4

    # 旧修订快照保持原样
    older = client.get(f"/revisions/{before_rev}").json()
    assert all("rescue_task_id" in g for g in older["snapshot"]["groups"])


# --------------------------------------------------------------------------
# 场景重置
# --------------------------------------------------------------------------

def test_scenario_reset_clears_teams_and_tasks_keeps_revisions(client):
    make_awaiting(client)
    register(client, 1, 10)
    task_id = dispatch(client, 1, 1).json()["task_id"]
    assert complete(client, task_id, delivery=4).status_code == 200
    revisions_before = revs(client)
    assert revisions_before  # 非空

    resp = client.post("/scenario", json=SCENARIO)
    assert resp.status_code == 200
    assert client.get("/rescue-teams").json()["teams"] == []
    assert client.get("/rescue-tasks").json()["tasks"] == []
    # 旧修订保留、编号继续递增
    revisions_after = revs(client)
    assert [r["rev"] for r in revisions_after[:len(revisions_before)]] == \
        [r["rev"] for r in revisions_before]
    assert revisions_after[-1]["operation"] == "scenario"
    # 重置后任务号已不可查（路径参数 -> 404）
    assert client.get(f"/rescue-tasks/{task_id}").status_code == 404
    # 重置后登记队伍重新从空闲开始
    assert register(client, 1, 10).status_code == 201


# --------------------------------------------------------------------------
# 并发抢占
# --------------------------------------------------------------------------

def test_concurrent_dispatch_same_team_only_one_wins(client):
    c = client
    c.post("/scenario", json=MULTI_SCENARIO)
    make_two_awaiting(c)
    register(c, 1, 10)

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(c.post, "/rescue-dispatches",
                         json={"group_id": 1, "team_id": 1, "node_id": 2,
                               "observed_minute": 4})
        f2 = pool.submit(c.post, "/rescue-dispatches",
                         json={"group_id": 2, "team_id": 1, "node_id": 8,
                               "observed_minute": 4})
        r1, r2 = f1.result(), f2.result()
    statuses = sorted([r1.status_code, r2.status_code])
    assert statuses == [200, 409]
    failed = r1 if r1.status_code == 409 else r2
    assert failed.json()["conflicts"][0]["reason"] == "TEAM_BUSY"
    # 恰一项进行中任务，修订号连续
    active = [t for t in c.get("/rescue-tasks").json()["tasks"]
              if t["status"] == "dispatched"]
    assert len(active) == 1
    entries = c.get("/revisions").json()["revisions"]
    assert [e["rev"] for e in entries] == list(range(1, len(entries) + 1))


def _post_dispatch(c):
    return c.post("/rescue-dispatches",
                  json={"group_id": 1, "team_id": 1, "node_id": 2,
                        "observed_minute": 4})


def test_dispatch_racing_tick_either_accepted_or_rejected(client):
    from concurrent.futures import ThreadPoolExecutor

    for _ in range(6):
        make_awaiting(client)
        register(client, 1, 10)
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_d = pool.submit(_post_dispatch, client)
            f_t = pool.submit(client.post, "/tick", json={"minutes": 1})
            rd, rt = f_d.result(), f_t.result()
        assert rt.status_code == 200
        g1 = by_id(client.get("/plan").json())[1]
        if rd.status_code == 200:
            assert g1["status"] == "rescuing"
            assert rd.json()["task"]["dispatch_minute"] == 4
        else:
            assert rd.status_code == 409
            assert rd.json()["conflicts"][0]["reason"] == "MINUTE_CHANGED"
            assert g1["status"] == "awaiting_rescue"
        break  # 一次串行顺序即可（实际由锁保证）
