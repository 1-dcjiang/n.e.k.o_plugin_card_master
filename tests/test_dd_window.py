"""dd_window 桌面窗口模块的纯逻辑测试（不启动 Tk，避免无显示环境依赖）。"""

import threading

from dd_window import DouDizhuWindow, card_display
from doudizhu import DouDizhuGame


def _plugin_with_game(landlord_idx: int = 0):
    """构造插件实例并开一局小游戏（不启动窗口）。

    注意：CardMasterPlugin 在函数内导入——插件包相对宿主树导入，
    顶层导入会因 isort 的 first-party 判定在本地/CI 之间产生分歧。
    """
    from plugin.plugins.card_master import CardMasterPlugin

    inst = CardMasterPlugin.__new__(CardMasterPlugin)
    inst._lock = threading.RLock()
    inst._game = None
    inst._game_log = []
    inst._live_sessions = {}
    inst._dd_game = None
    inst._dd_stats = {"wins": 0, "losses": 0, "role": "auto"}
    inst._dd_window = None
    g = DouDizhuGame(["你", "YUI_左", "YUI_右"])
    g._set_landlord(landlord_idx, 2)
    inst._dd_game = g
    inst._dd_stats["role"] = "landlord" if landlord_idx == 0 else "farmer"
    return inst


def test_card_display_small():
    # 3♠ = 3*4+0
    text, color = card_display(3 * 4 + 0)
    assert text == "3♠"
    assert color == "black"


def test_card_display_red_suit():
    # 10♥ = 10*4+1
    text, color = card_display(10 * 4 + 1)
    assert text == "10♥"
    assert color == "red"


def test_card_display_jokers():
    # 小王 16*4，大王 17*4
    small, c1 = card_display(16 * 4)
    assert small == "小王" and c1 == "black"
    big, c2 = card_display(17 * 4)
    assert big == "大王" and c2 == "red"


def test_window_constructible_without_tk():
    """构造窗口对象不应触发 Tk（Tk 只在 run() 里创建）。"""
    win = DouDizhuWindow(on_action=lambda op: None, on_close=lambda: None)
    assert win.is_alive_ok() is False  # 未 start，视为不可用
    # 未启动时 push 不应抛错（队列可写入；close 事件未设置）
    win.push({"op": "render", "state": {}})
    assert win.is_alive() is False


def test_window_state_shape():
    """_dd_window_state 组装窗口渲染数据：字段齐全、must_play 正确。"""
    inst = _plugin_with_game(landlord_idx=0)
    st = inst._dd_window_state(inst._dd_game)
    for key in ("stage", "user_role", "user_hand", "bottom", "last_play",
                "last_player", "players", "current", "must_play", "message", "stats"):
        assert key in st
    # 你是地主先手且无上一手 → must_play
    assert st["must_play"] is True
    assert st["user_role"] == "地主"
    assert len(st["players"]) == 3
    assert st["current"] == "你"
    # 手牌应为 20 张（17 + 3 底牌）
    assert len(st["user_hand"]) == 20


def test_window_state_must_play_after_play():
    """出过牌（有 last_play）后不再 must_play。"""
    inst = _plugin_with_game(landlord_idx=0)
    g = inst._dd_game
    # 你是地主先手：出单张后应更新 last_play
    first = g.players[0]["hand"][0]
    r = g.play(0, [first])
    assert r.get("ok")
    st = inst._dd_window_state(g)
    assert st["must_play"] is False
    assert st["last_play"] == [first]


def test_window_on_action_play_wins_updates_stats():
    """窗口回调：你出完最后一张直接获胜 → 战绩必须更新（回归：曾漏记）。"""
    inst = _plugin_with_game(landlord_idx=0)
    g = inst._dd_game
    # 摆成只剩一对 3 的局面
    g.players[0]["hand"] = [3 * 4, 3 * 4 + 1]
    g.players[1]["hand"] = [5 * 4]
    g.players[2]["hand"] = [6 * 4]
    g.last_play = []
    g.last_player = None
    g.current = 0
    wins_before = inst._dd_stats["wins"]
    inst._dd_window_on_action({"op": "play", "cards": [3 * 4, 3 * 4 + 1]})
    assert g.stage == "over"
    assert inst._dd_stats["wins"] == wins_before + 1


def test_window_on_action_pass_no_crash():
    """窗口回调：'不要'在轮到用户时正常推进，不抛错。"""
    inst = _plugin_with_game(landlord_idx=0)
    g = inst._dd_game
    # 制造一个'你'需要跟的局面：先手 AI 出了一张，轮到你
    g.current = 0
    g.last_play = [g.players[0]["hand"][0]]
    g.last_player = 1
    # 直接调 pass（轮到用户，有上一手）
    g.current = 0
    inst._dd_window_on_action({"op": "pass"})
    # 不应抛错；pass 后回合流转
    assert inst._dd_game is g


def test_hand_layout_shrinks_for_many_cards():
    """手牌多时自适应缩宽，防止超出画布。"""
    win = DouDizhuWindow(on_action=lambda op: None, on_close=lambda: None)
    win._user_hand = list(range(20))  # 20 张（地主满手）
    w, x_start = win._hand_layout()
    assert w < 44  # 缩窄
    # 画布宽 780，总宽不得超出
    assert x_start >= 0 and x_start + w * 20 <= 780
    # 少牌时保持标准宽度
    win._user_hand = [3 * 4]
    w2, x_start2 = win._hand_layout()
    assert w2 == 44 and x_start2 >= 0
