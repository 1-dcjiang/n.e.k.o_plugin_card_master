"""dd_window 桌面窗口模块的纯逻辑测试（不启动 Tk，避免无显示环境依赖）。"""

from dd_window import DouDizhuWindow, card_display


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
