"""斗地主小游戏桌面窗口（欢乐斗地主风格，tkinter）。

线程模型：
- 窗口在独立线程运行 Tk 主循环（Windows 支持非主线程创建 Tk）。
- 插件 → 窗口：`push(op)` 放入指令队列，UI 线程轮询渲染。
- 窗口 → 插件：按钮回调调用 `on_action(op)`（在 UI 线程执行），
  由插件在锁保护下推进游戏并触发下一次渲染。

窗口不可用时（无显示环境/导入失败）全部调用静默降级，不影响文字玩法。
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import font as tkfont
from typing import Callable

# 牌面映射
SUIT_SYMBOLS = {0: "♠", 1: "♥", 2: "♦", 3: "♣"}
RANK_NAMES = {3: "3", 4: "4", 5: "5", 6: "6", 7: "7", 8: "8", 9: "9",
              10: "10", 11: "J", 12: "Q", 13: "K", 14: "A", 15: "2",
              16: "小王", 17: "大王"}
RED_SUITS = {1, 2}  # ♥ ♦ 为红色

# 布局常量
CARD_W, CARD_H = 44, 62
HAND_Y = 470          # 用户手牌行
SELECT_DY = 14        # 选中上移
AI_CARD_W, AI_CARD_H = 30, 42
PAD = 10

CANVAS_W, CANVAS_H = 780, 560


def card_display(card: int) -> tuple[str, str]:
    """返回 (显示文本, 颜色)。card 编码 rank*4+suit，16=小王 17=大王。"""
    rank = card // 4
    suit = card % 4
    if rank >= 16:
        name = "小王" if rank == 16 else "大王"
        return name, "red" if rank == 17 else "black"
    return f"{RANK_NAMES[rank]}{SUIT_SYMBOLS[suit]}", ("red" if suit in RED_SUITS else "black")


class DouDizhuWindow(threading.Thread):
    """欢乐斗地主风格窗口。on_action: Callable[[dict], None]，在 UI 线程回调。"""

    def __init__(self, on_action: Callable[[dict], None], on_close: Callable[[], None]):
        super().__init__(daemon=True)
        self._q: "queue.Queue[dict]" = queue.Queue()
        self._on_action = on_action
        self._on_close = on_close
        self._root: tk.Tk | None = None
        self._canvas: tk.Canvas | None = None
        self._state: dict | None = None
        self._selected: set[int] = set()  # 选中牌（用户手牌中的原始牌 int）
        self._user_hand: list[int] = []
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._stage_label: tk.Label | None = None
        self._msg_label: tk.Label | None = None
        self._play_btn: tk.Button | None = None
        self._pass_btn: tk.Button | None = None
        self._status_label: tk.Label | None = None

    # ── 供插件线程调用 ──────────────────────────────

    def push(self, op: dict) -> None:
        """从任意线程发送指令（render/close）。非阻塞。"""
        if self._closed.is_set():
            return
        try:
            self._q.put(op, block=False)
        except queue.Full:  # 队列满丢弃，下一次 render 会覆盖
            pass

    def push_render(self, state: dict) -> None:
        self.push({"op": "render", "state": state})

    def is_alive_ok(self) -> bool:
        return not self._closed.is_set() and self.is_alive()

    def request_close(self) -> None:
        self.push({"op": "close"})

    # ── 线程体 ─────────────────────────────────────

    def run(self) -> None:
        try:
            self._root = tk.Tk()
        except Exception:
            self._closed.set()
            return
        self._root.title("斗地主小游戏 · 卡牌大师")
        self._root.resizable(False, False)
        self._build_ui()
        self._ready.set()
        try:
            self._root.after(60, self._poll)
            self._root.mainloop()
        finally:
            self._closed.set()
            try:
                self._on_close()
            except Exception:
                pass

    def _build_ui(self) -> None:
        root = self._root
        root.configure(bg="#2c7a2c")
        f_title = tkfont.Font(size=12, weight="bold")
        f_info = tkfont.Font(size=10)

        self._canvas = tk.Canvas(root, width=CANVAS_W, height=CANVAS_H,
                                 bg="#1d6e1d", highlightthickness=0)
        self._canvas.pack(padx=8, pady=(8, 0))
        self._canvas.bind("<Button-1>", self._on_canvas_click)

        bar = tk.Frame(root, bg="#2c7a2c")
        bar.pack(fill="x", padx=10, pady=6)
        self._stage_label = tk.Label(bar, text="斗地主小游戏", font=f_title,
                                     bg="#2c7a2c", fg="white")
        self._stage_label.pack(side="left")
        self._status_label = tk.Label(bar, text="", font=f_info,
                                      bg="#2c7a2c", fg="#ffe08a")
        self._status_label.pack(side="left", padx=14)

        btns = tk.Frame(bar, bg="#2c7a2c")
        btns.pack(side="right")
        self._play_btn = tk.Button(btns, text="出 牌", width=7,
                                   command=self._do_play, state="disabled")
        self._play_btn.pack(side="left", padx=4)
        self._pass_btn = tk.Button(btns, text="不 要", width=7,
                                   command=self._do_pass, state="disabled")
        self._pass_btn.pack(side="left", padx=4)
        tk.Button(btns, text="再来一局", width=8,
                  command=lambda: self._on_action({"op": "new_game", "role": "auto"})).pack(side="left", padx=4)

        self._msg_label = tk.Label(root, text="", font=f_info, bg="#2c7a2c",
                                   fg="#d9f2d9", justify="left", anchor="w")
        self._msg_label.pack(fill="x", padx=12, pady=(2, 8))

    def _poll(self) -> None:
        """轮询指令队列并刷新。AI 回合时自动请求插件推进（tick）。"""
        try:
            while True:
                op = self._q.get_nowait()
                self._handle_op(op)
        except queue.Empty:
            pass
        # 自动出牌：playing 且不是你的回合 → 请求插件让 AI 出一步
        st = self._state
        if st and st.get("stage") == "playing" and st.get("current") not in ("你", ""):
            try:
                self._on_action({"op": "tick"})
            except Exception:
                pass
        if not self._closed.is_set():
            self._root.after(60, self._poll)

    def _handle_op(self, op: dict) -> None:
        kind = op.get("op")
        if kind == "render":
            self._state = op.get("state")
            self._sync_selection()
            self._draw()
        elif kind == "close":
            try:
                self._root.destroy()
            except Exception:
                pass

    # ── 渲染 ──────────────────────────────────────

    def _sync_selection(self) -> None:
        """用户手牌变化后清除失效选中。"""
        if not self._state:
            return
        hand = self._state.get("user_hand", [])
        if hand != self._user_hand:
            self._user_hand = list(hand)
            self._selected = set()

    def _draw(self) -> None:
        c = self._canvas
        if c is None:
            return
        c.delete("all")
        st = self._state or {}
        stage = st.get("stage", "bidding")
        my_role = st.get("user_role", "农民")
        bottom = st.get("bottom", [])
        last_play = st.get("last_play", [])
        last_player = st.get("last_player", "")
        players = st.get("players", [])
        current = st.get("current", "")
        hand = st.get("user_hand", [])
        msg = st.get("message", "")
        stats = st.get("stats", "")

        # 顶部标题与底牌
        c.create_text(CANVAS_W // 2, 26, text="斗地主小游戏",
                      fill="white", font=tkfont.Font(size=14, weight="bold"))
        self._stage_label.config(text=f"身份：{my_role}" + ("（地主）" if my_role == "地主" else ""))
        self._status_label.config(text=f"轮到：{current}　底牌：{' '.join(card_display(x)[0] for x in bottom) if bottom else '（未揭晓）'}")

        # 左右 AI
        for idx, side in ((1, "left"), (2, "right")):
            p = players[idx] if len(players) > idx else {}
            name = p.get("name", f"AI_{idx}")
            cnt = p.get("count", 0)
            role_txt = "地主" if p.get("role") == "地主" else "农民"
            if side == "left":
                x0, y0, x1, y1 = 24, 150, 24 + AI_CARD_W, 150 + AI_CARD_H
            else:
                x0, y0, x1, y1 = CANVAS_W - 24 - AI_CARD_W, 150, CANVAS_W - 24, 150 + AI_CARD_H
            c.create_rectangle(x0, y0, x1, y1, fill="#2556b5", outline="white", width=2)
            c.create_text((x0 + x1) // 2, (y0 + y1) // 2, text=str(cnt), fill="white",
                          font=tkfont.Font(size=11, weight="bold"))
            c.create_text((x0 + x1) // 2, y1 + 14, text=f"{name}（{role_txt}）",
                          fill="white", font=tkfont.Font(size=9))

        # 中间出牌区（最近一手）
        if last_play:
            c.create_text(CANVAS_W // 2, 250, text=f"上一手（{last_player}）：",
                          fill="#ffe08a", font=tkfont.Font(size=10, weight="bold"))
            n = len(last_play)
            w = min(CARD_W, (CANVAS_W - 40) // max(1, n))
            self._draw_cards(c, last_play, CANVAS_W // 2 - w * n // 2, 272, w, CARD_H)

        # 底部用户手牌
        if hand:
            total_w = CARD_W * len(hand)
            x_start = max(10, (CANVAS_W - total_w) // 2)
            for i, card in enumerate(hand):
                x = x_start + i * CARD_W
                y = HAND_Y - (SELECT_DY if card in self._selected else 0)
                self._draw_single(c, card, x, y, CARD_W, CARD_H)

        # 阶段消息 / 胜负
        if stage == "bidding":
            c.create_text(CANVAS_W // 2, 380, text="叫地主阶段…", fill="white",
                          font=tkfont.Font(size=12, weight="bold"))
        elif stage == "over":
            c.create_text(CANVAS_W // 2, 330, text=msg or "本局结束",
                          fill="#ffd34d", font=tkfont.Font(size=15, weight="bold"))
            if stats:
                c.create_text(CANVAS_W // 2, 358, text=stats, fill="white",
                              font=tkfont.Font(size=11))
        else:
            if current == "你":
                c.create_text(CANVAS_W // 2, 330, text="轮到你出牌！",
                              fill="#ffd34d", font=tkfont.Font(size=13, weight="bold"))
            else:
                c.create_text(CANVAS_W // 2, 330, text=f"等待 {current} 出牌…",
                              fill="white", font=tkfont.Font(size=12))

        # 按钮状态
        can_act = stage == "playing" and current == "你"
        must_play = bool(st.get("must_play"))  # 新一轮先手，必须出牌不能不要
        self._play_btn.config(state="normal" if (can_act and self._selected) else "disabled")
        self._pass_btn.config(state="normal" if (can_act and not must_play) else "disabled")

    def _draw_single(self, c: tk.Canvas, card: int, x: int, y: int, w: int, h: int) -> None:
        text, color = card_display(card)
        c.create_rectangle(x, y, x + w, y + h, fill="white", outline="#888",
                           width=1, tags="card")
        c.create_text(x + 5, y + 5, text=text, fill=color, anchor="nw",
                      font=tkfont.Font(size=10 if w < 40 else 11, weight="bold"))

    def _draw_cards(self, c: tk.Canvas, cards: list[int], x: int, y: int, w: int, h: int) -> None:
        for i, card in enumerate(cards):
            self._draw_single(c, card, x + i * w, y, w, h)

    # ── 交互 ──────────────────────────────────────

    def _on_canvas_click(self, event) -> None:
        if not self._state or not self._user_hand:
            return
        st = self._state
        if st.get("stage") != "playing" or st.get("current") != "你":
            return
        x = event.x
        total_w = CARD_W * len(self._user_hand)
        x_start = max(10, (CANVAS_W - total_w) // 2)
        idx = (x - x_start) // CARD_W
        if 0 <= idx < len(self._user_hand):
            card = self._user_hand[idx]
            if card in self._selected:
                self._selected.discard(card)
            else:
                self._selected.add(card)
            self._draw()

    def _do_play(self) -> None:
        if not self._selected:
            return
        cards = sorted(self._selected)
        self._selected = set()
        self._on_action({"op": "play", "cards": cards})

    def _do_pass(self) -> None:
        self._selected = set()
        self._on_action({"op": "pass"})
