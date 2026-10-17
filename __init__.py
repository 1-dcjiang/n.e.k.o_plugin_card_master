"""card_master — 让 YUI 自主学习打牌（德州扑克 / Kards / 掼蛋）。

提供：
- 德州扑克：完整牌局模拟、牌型评估、胜率计算（蒙特卡洛）、出牌建议、自我对弈
- Kards：真实规则查询（来自官方 wiki）、能力关键词、国家特色、卡组合法性检查
- 掼蛋：规则查询
- 通用：任意 5/7 张牌型评估

设计对齐 neko-plugin-dev 规范：
- 工具签名 async def xxx(self, **_)，返回 dict
- 牌局状态存内存（插件重启后丢失，属预期）
- 描述写全"何时用/前置/限制"，错误给"下一步做什么"
"""
from __future__ import annotations

import asyncio
import json
import threading

from plugin.sdk.plugin import (
    NekoPluginBase,
    lifecycle,
    llm_tool,
)

from . import kards as kards_mod
from .engine import (
    TexasHoldemGame,
    calculate_odds,
    evaluate_hand,
    hand_str,
    parse_card,
    self_play,
    suggest_action,
)

# ══════════ 掼蛋规则（简要，标准规则） ══════════

GUANDAN_RULES = """## 掼蛋基本规则

**人数**：4 人，对家组队（2 对 2）。
**牌数**：两副扑克牌共 108 张（含 4 张王：2 小王 + 2 大王）。
**目标**：升级制，从打 2 开始，先打过 A 的一方获胜。

### 牌型（从大到小）
- **四王炸**：4 张王（最大牌型）
- **炸弹**：4 张及以上同点数牌（张数越多越大；6张以上炸弹叫"钢板"，可压同花顺）
- **同花顺**：5 张及以上同花色连续牌
- **顺子**：5 张及以上连续牌（不限花色）
- **三带二**：3 张同点 + 2 张同点（如 888+55）
- **三张**：3 张同点
- **对子**：2 张同点
- **单张**：1 张牌

### 核心机制
- **逢人配（百搭）**：当前所打级牌的红桃牌是"逢人配"，可当任意牌使用（不能配王）。例如打 2 时，红桃 2 是逢人配。
- **升级**：一对中两人分获上游（第1名）和二游（第2名）→ 连升 3 级；上游+三游 → 升 2 级；上游+下游 → 升 1 级。
- **双下**：一对两人分列三、四名（双下游）→ 对方升级，本方下局需进贡。
- **进贡/还贡**：下游向上游进贡最大的单张牌（王除外），上游还一张任意牌。
- **抗贡**：下游手中有 2 张大王可抗贡（不进贡）。

### 出牌规则
- 上游先出，逆时针轮流出牌
- 只能用同牌型压（炸弹/四王炸可压任何牌型）
- 一轮中所有人都不要则该轮最大者获得下一轮出牌权
"""

# ══════════ 插件主类 ══════════


class CardMasterPlugin(NekoPluginBase):
    """卡牌大师 — 让 YUI 自主学习和练习各类卡牌游戏。"""

    def __init__(self, ctx):
        super().__init__(ctx)
        self._game: TexasHoldemGame | None = None
        self._lock = threading.Lock()
        self._game_log: list[dict] = []

    # ══════════ 生命周期 ══════════

    @lifecycle(id="startup")
    async def on_startup(self, **_):
        return {"status": "ready", "plugin": "card_master"}

    @lifecycle(id="shutdown")
    async def on_shutdown(self, **_):
        with self._lock:
            self._game = None
            self._game_log.clear()
        return {"status": "shutdown"}

    # ══════════ 工具：规则查询 ══════════

    @llm_tool(
        name="card_rules",
        description="查询卡牌游戏规则。何时用：用户问'德州扑克怎么玩''Kards规则''掼蛋怎么打'或需要了解某游戏规则时。参数 game：poker（德州扑克）/ kards / guandan（掼蛋）/ all；topic：可选子主题（Kards 支持 basic/resource/battlefield/card_types/unit_types/learning，不填返回全部）。",
        parameters={"type": "object", "properties": {
            "game": {"type": "string", "description": "游戏：poker / kards / guandan / all"},
            "topic": {"type": "string", "description": "可选子主题（Kards 用）"},
        }, "required": ["game"]},
        timeout=30.0,
    )
    async def card_rules(self, *, game: str = "", topic: str = "", **_):
        params = _.get("parameters") or {}
        game = (game or params.get("game") or "").lower().strip()
        topic = (topic or params.get("topic") or "").strip()

        if game in ("poker", "德州扑克", "texas", "德扑"):
            return {"output": self._poker_rules(), "is_error": False}
        if game in ("kards",):
            return {"output": kards_mod.get_rules(topic), "is_error": False}
        if game in ("guandan", "掼蛋", "guandan"):
            return {"output": GUANDAN_RULES, "is_error": False}
        if game in ("all", "全部", ""):
            return {"output": self._poker_rules() + "\n\n---\n\n" + kards_mod.get_rules("all") + "\n\n---\n\n" + GUANDAN_RULES,
                    "is_error": False}
        return {"output": f"未知游戏 '{game}'。支持：poker（德州扑克）/ kards / guandan（掼蛋）/ all",
                "is_error": True, "error": "UNKNOWN_GAME"}

    def _poker_rules(self) -> str:
        return """## 德州扑克（Texas Hold'em）基本规则

**人数**：2-10 人。
**目标**：赢取底池——要么让对手弃牌，要么摊牌时拥有最大牌型。

**牌型（从大到小）**：
皇家同花顺 > 同花顺 > 四条 > 葫芦（三带二）> 同花 > 顺子 > 三条 > 两对 > 一对 > 高牌

**游戏流程**：
1. **翻牌前（Preflop）**：每人发 2 张底牌；小盲/大盲下盲注；从大盲下家开始行动（弃牌/跟注/加注）
2. **翻牌（Flop）**：发 3 张公共牌，再次下注轮
3. **转牌（Turn）**：发第 4 张公共牌，下注轮
4. **河牌（River）**：发第 5 张公共牌，最后下注轮
5. **摊牌（Showdown）**：未弃牌玩家用 2 张底牌 + 5 张公共牌中选最佳 5 张比大小

**行动选项**：弃牌（fold）/ 过牌（check，无需跟注时）/ 跟注（call）/ 加注（raise）/ 全下（all-in）

**关键概念**：
- 底池赔率（Pot Odds）：跟注额 / (底池 + 跟注额)，胜率高于底池赔率则跟注有利
- 位置（Position）：越后行动越有信息优势
- 底牌范围（Range）：推测对手可能持有的牌组合
"""

    # ══════════ 工具：牌型评估 ══════════

    @llm_tool(
        name="card_evaluate",
        description="评估手牌牌型。何时用：用户问'我这手牌是什么牌型''这7张牌能组成什么'或需要分析手牌强度时。参数 hand：手牌字符串，空格分隔，如 'As Kh'；community：公共牌（可选），如 'Td Js Qh'；自动从 5 或 7 张中选最佳 5 张。牌格式：点数(2-9,T,J,Q,K,A)+花色(s=♠,h=♥,d=♦,c=♣)，如 As=黑桃A, Td=方块10。",
        parameters={"type": "object", "properties": {
            "hand": {"type": "string", "description": "手牌，空格分隔，如 'As Kh'"},
            "community": {"type": "string", "description": "公共牌（可选），空格分隔"},
        }, "required": ["hand"]},
        timeout=30.0,
    )
    async def card_evaluate(self, *, hand: str = "", community: str = "", **_):
        params = _.get("parameters") or {}
        hand = hand or params.get("hand", "")
        community = community or params.get("community", "")
        try:
            hand_cards = [parse_card(c) for c in hand.split()]
            comm_cards = [parse_card(c) for c in community.split()] if community else []
            all_cards = hand_cards + comm_cards
            if len(all_cards) < 5:
                return {"output": f"至少需要 5 张牌，当前 {len(all_cards)} 张。请补充手牌或公共牌。",
                        "is_error": True, "error": "NOT_ENOUGH_CARDS"}
            result = evaluate_hand(all_cards)
            return {
                "output": f"牌型：{result['name']}\n最佳5张：{result['cards_str']}\n输入：{hand_str(all_cards)}",
                "is_error": False,
                "detail": result,
            }
        except ValueError as e:
            return {"output": f"牌格式错误：{e}。正确格式如 'As Kh'（点数+花色，s=♠ h=♥ d=♦ c=♣）",
                    "is_error": True, "error": "INVALID_CARD"}

    # ══════════ 工具：创建牌局 ══════════

    @llm_tool(
        name="card_new_game",
        description="创建一局新的德州扑克牌局并发牌。何时用：用户说'开一局德州扑克''我们来打牌'或需要开始模拟牌局时。参数 players：玩家列表，默认6个AI（可指定名字和筹码）；small_blind：小盲注，默认10；big_blind：大盲注，默认20；seed：随机种子（可选，相同种子可复现牌局）。创建后用 card_action 出牌、card_state 看状态、card_suggest 获取建议。",
        parameters={"type": "object", "properties": {
            "players": {"type": "string", "description": "可选，JSON数组，如 '[{\"name\":\"玩家1\",\"chips\":1000},{\"name\":\"玩家2\",\"chips\":1000}]'。不填则默认6个AI"},
            "small_blind": {"type": "integer", "description": "小盲注，默认10"},
            "big_blind": {"type": "integer", "description": "大盲注，默认20"},
            "seed": {"type": "integer", "description": "随机种子（可选）"},
        }},
        timeout=30.0,
    )
    async def card_new_game(self, *, players: str = "", small_blind: int = 10,
                             big_blind: int = 20, seed: int = 0, **_):
        params = _.get("parameters") or {}
        players_str = players or params.get("players", "")
        sb = int(small_blind or params.get("small_blind", 10))
        bb = int(big_blind or params.get("big_blind", 20))
        sd = int(seed or params.get("seed", 0)) or None

        try:
            if players_str:
                player_list = json.loads(players_str)
            else:
                player_list = [{"name": f"AI_{i+1}", "chips": 1000} for i in range(6)]
        except (json.JSONDecodeError, TypeError) as e:
            return {"output": f"players 格式错误：{e}。应为 JSON 数组，如 '[{{\"name\":\"A\",\"chips\":1000}}]'",
                    "is_error": True, "error": "INVALID_PLAYERS"}

        try:
            with self._lock:
                self._game = TexasHoldemGame(player_list, sb, bb, sd)
                self._game_log = []
            state = self._game.get_state()
            return {
                "output": f"新牌局已创建！{len(player_list)} 人，小盲{sb}/大盲{bb}。\n当前阶段：{state['stage']}\n当前玩家：{state['current_player']}（手牌 {state['current_player_hand']}，筹码 {state['current_player_chips']}）\n底池：{state['pot']}，需跟注：{state['to_call']}\n用 card_action 出牌，或 card_suggest 获取建议。",
                "is_error": False,
                "game_state": state,
            }
        except Exception as e:
            return {"output": f"创建牌局失败：{e}", "is_error": True, "error": "CREATE_FAILED"}

    # ══════════ 工具：出牌 ══════════

    @llm_tool(
        name="card_action",
        description="在当前德州扑克牌局中执行出牌行动。何时用：需要打出一手牌时（弃牌/过牌/跟注/加注/全下）。参数 action：fold（弃牌）/ check（过牌）/ call（跟注）/ raise（加注，需指定amount为加注后总额）/ all_in（全下）；amount：加注时的总额（含已下注，如当前bet=20，加注到60则amount=60）。前置：需先用 card_new_game 创建牌局。",
        parameters={"type": "object", "properties": {
            "action": {"type": "string", "description": "fold / check / call / raise / all_in"},
            "amount": {"type": "integer", "description": "raise 时的加注后总额"},
        }, "required": ["action"]},
        timeout=30.0,
    )
    async def card_action(self, *, action: str = "", amount: int = 0, **_):
        params = _.get("parameters") or {}
        action = (action or params.get("action", "")).lower().strip()
        amt = int(amount or params.get("amount", 0))

        with self._lock:
            if self._game is None:
                return {"output": "当前没有牌局，请先用 card_new_game 创建。", "is_error": True, "error": "NO_GAME"}
            result = self._game.action(action, amount=amt)

        if not result.get("ok"):
            return {"output": f"行动失败：{result.get('error')}", "is_error": True, "error": "ACTION_FAILED"}

        msg = result.get("message", "")
        if result.get("result"):
            r = result["result"]
            if r.get("winners"):
                winners = ", ".join(f"{w['name']}({w['hand']})" for w in r["winners"])
                msg += f"\n摊牌结果：{winners} 赢得底池 {r['pot']}"
            elif r.get("winner"):
                msg += f"\n{r['winner']} 赢得底池 {r['pot']}"

        state = result.get("game_state") or (self._game.get_state() if self._game else None)
        if state and state.get("stage") != "showdown":
            msg += f"\n\n当前：{state['current_player']}（{state['current_player_hand']}），阶段 {state['stage']}，底池 {state['pot']}，需跟注 {state['to_call']}"

        return {"output": msg, "is_error": False, "game_state": state, "result": result.get("result")}

    # ══════════ 工具：牌局状态 ══════════

    @llm_tool(
        name="card_state",
        description="查询当前德州扑克牌局状态。何时用：需要了解当前牌局进展（阶段、底池、玩家手牌、筹码、该谁行动）时。前置：需先用 card_new_game 创建牌局。",
        parameters={"type": "object", "properties": {}},
        timeout=30.0,
    )
    async def card_state(self, **_):
        with self._lock:
            if self._game is None:
                return {"output": "当前没有牌局，请先用 card_new_game 创建。", "is_error": True, "error": "NO_GAME"}
            state = self._game.get_state()

        lines = [
            f"第 {state['hand_number']} 手牌 | 阶段：{state['stage']}",
            f"公共牌：{state['community']}（{state['community_count']}张）",
            f"底池：{state['pot']} | 当前下注：{state['current_bet']} | 需跟注：{state['to_call']}",
            f"当前玩家：{state['current_player']}（手牌 {state['current_player_hand']}，筹码 {state['current_player_chips']}）",
            "",
            "玩家状态：",
        ]
        for p in state["players"]:
            status = []
            if p["folded"]:
                status.append("已弃牌")
            if p["all_in"]:
                status.append("全下")
            if p["has_acted"]:
                status.append("已行动")
            hand = p["hand"] if p["name"] == state["current_player"] else "（背面）"
            lines.append(f"  {p['name']}: 筹码{p['chips']} 下注{p['bet']} 手牌{hand} {' '.join(status)}")

        return {"output": "\n".join(lines), "is_error": False, "game_state": state}

    # ══════════ 工具：胜率计算 ══════════

    @llm_tool(
        name="card_odds",
        description="计算当前手牌的胜率（蒙特卡洛模拟）。何时用：需要知道'我这手牌赢的概率有多少'或做出牌决策前评估胜率时。参数 opponents：对手数量，默认1；simulations：模拟次数，默认2000（越多越准但越慢）。前置：需先用 card_new_game 创建牌局，自动读取当前玩家手牌和公共牌。也可直接传 hand 和 community 参数手动指定。",
        parameters={"type": "object", "properties": {
            "opponents": {"type": "integer", "description": "对手数量，默认1"},
            "simulations": {"type": "integer", "description": "模拟次数，默认2000"},
            "hand": {"type": "string", "description": "可选，手动指定手牌如 'As Kh'（不填则用当前牌局）"},
            "community": {"type": "string", "description": "可选，手动指定公共牌"},
        }},
        timeout=120.0,
    )
    async def card_odds(self, *, opponents: int = 1, simulations: int = 2000,
                         hand: str = "", community: str = "", **_):
        params = _.get("parameters") or {}
        opp = int(opponents or params.get("opponents", 1))
        sim = int(simulations or params.get("simulations", 2000))
        hand_str_in = hand or params.get("hand", "")
        comm_str_in = community or params.get("community", "")

        try:
            if hand_str_in:
                hand_cards = [parse_card(c) for c in hand_str_in.split()]
                comm_cards = [parse_card(c) for c in comm_str_in.split()] if comm_str_in else []
            else:
                with self._lock:
                    if self._game is None:
                        return {"output": "当前没有牌局，请先用 card_new_game 创建，或手动传 hand 参数。",
                                "is_error": True, "error": "NO_GAME"}
                    p = self._game.players[self._game.current]
                    hand_cards = list(p["hand"])
                    comm_cards = list(self._game.community)

            if len(hand_cards) != 2:
                return {"output": f"需要恰好2张底牌，当前 {len(hand_cards)} 张。",
                        "is_error": True, "error": "INVALID_HAND"}

            result = calculate_odds(hand_cards, comm_cards, opponents=opp, simulations=sim)
            return {
                "output": (f"手牌 {result['your_hand']} | 公共牌 {result['community']}\n"
                           f"对手数：{result['opponents']} | 模拟：{result['simulations']} 次\n"
                           f"胜率：{result['win_rate']}% | 平局：{result['tie_rate']}% | 落败：{result['lose_rate']}%"),
                "is_error": False,
                "detail": result,
            }
        except ValueError as e:
            return {"output": f"牌格式错误：{e}", "is_error": True, "error": "INVALID_CARD"}

    # ══════════ 工具：出牌建议 ══════════

    @llm_tool(
        name="card_suggest",
        description="基于当前牌局给出出牌建议（综合胜率、底池赔率、位置分析）。何时用：不知道该怎么出牌、需要AI建议时。返回建议行动（fold/check/call/raise）+ 理由 + 胜率 + 底池赔率。前置：需先用 card_new_game 创建牌局。",
        parameters={"type": "object", "properties": {
            "simulations": {"type": "integer", "description": "胜率模拟次数，默认1500"},
        }},
        timeout=120.0,
    )
    async def card_suggest(self, *, simulations: int = 1500, **_):
        params = _.get("parameters") or {}
        sim = int(simulations or params.get("simulations", 1500))

        with self._lock:
            if self._game is None:
                return {"output": "当前没有牌局，请先用 card_new_game 创建。", "is_error": True, "error": "NO_GAME"}
            if self._game.stage == "showdown":
                return {"output": "牌局已结束，请用 card_new_game 开新一局。", "is_error": True, "error": "GAME_OVER"}
            sug = suggest_action(self._game, simulations=sim)

        action_map = {"fold": "弃牌", "check": "过牌", "call": "跟注", "raise": "加注"}
        action_cn = action_map.get(sug["suggestion"], sug["suggestion"])
        raise_info = f"，加注到 {sug['raise_to']}" if sug.get("raise_to") else ""
        exec_hint = f"action={sug['suggestion']}"
        if sug.get("raise_to"):
            exec_hint += f", amount={sug['raise_to']}"

        return {
            "output": (f"建议：{action_cn}{raise_info}\n"
                       f"理由：{sug['reason']}\n"
                       f"手牌 {sug['your_hand']} | 公共牌 {sug['community']}\n"
                       f"胜率 {sug['win_rate']}% | 平局 {sug['tie_rate']}% | 有效胜率 {sug['equity']}%\n"
                       f"底池 {sug['pot']} | 需跟注 {sug['to_call']} | {sug['pot_odds_detail']}\n"
                       f"（用 card_action 执行：{exec_hint}）"),
            "is_error": False,
            "suggestion": sug,
        }

    # ══════════ 工具：自我对弈 ══════════

    @llm_tool(
        name="card_selfplay",
        description="AI 自我对弈训练，让 YUI 通过观察 AI 对局学习打牌。何时用：用户说'自我对弈''自己打一局学习''训练AI打牌'时。参数 game：目前支持 poker（德州扑克）；rounds：对弈几手牌，默认1；players：玩家数（2-10），默认6；seed：随机种子（可选）；verbose：是否输出详细每步日志，默认true。返回每手牌结果、最终筹码排名、赢家。",
        parameters={"type": "object", "properties": {
            "game": {"type": "string", "description": "游戏类型，目前支持 poker"},
            "rounds": {"type": "integer", "description": "对弈手数，默认1"},
            "players": {"type": "integer", "description": "玩家数2-10，默认6"},
            "seed": {"type": "integer", "description": "随机种子（可选）"},
            "verbose": {"type": "boolean", "description": "是否输出详细日志，默认true"},
        }},
        timeout=300.0,
    )
    async def card_selfplay(self, *, game: str = "poker", rounds: int = 1,
                             players: int = 6, seed: int = 0, verbose: bool = True, **_):
        params = _.get("parameters") or {}
        g = (game or params.get("game", "poker")).lower()
        r = int(rounds or params.get("rounds", 1))
        p = int(players or params.get("players", 6))
        sd = int(seed or params.get("seed", 0)) or None
        vb = bool(verbose if verbose is not None else params.get("verbose", True))

        if g not in ("poker", "texas", "德州扑克"):
            return {"output": f"目前自我对弈仅支持 poker（德州扑克），'{g}' 暂不支持。",
                    "is_error": True, "error": "UNSUPPORTED_GAME"}
        if p < 2 or p > 10:
            return {"output": f"玩家数需 2-10，当前 {p}。", "is_error": True, "error": "INVALID_PLAYERS"}

        player_list = [{"name": f"AI_{i+1}", "chips": 1000} for i in range(p)]
        result = await asyncio.to_thread(self_play, player_list, rounds=r, seed=sd, verbose=vb)

        lines = [
            f"自我对弈完成：{result['rounds_played']} 手牌，{p} 名 AI",
            f"最终赢家：{result['winner']}",
            "",
            "最终筹码排名：",
        ]
        for i, c in enumerate(result["final_chips"], 1):
            lines.append(f"  {i}. {c['name']}: {c['chips']} 筹码")

        if result.get("results"):
            lines.append("")
            lines.append("各手牌结果：")
            for res in result["results"]:
                r = res.get("result", {})
                if r.get("winners"):
                    w = ", ".join(f"{x['name']}({x['hand']})" for x in r["winners"])
                    lines.append(f"  第{res['round']}手：{w} 赢 {r.get('pot', '?')}")
                elif r.get("winner"):
                    lines.append(f"  第{res['round']}手：{r['winner']} 赢 {r.get('pot', '?')}（弃牌结束）")

        return {"output": "\n".join(lines), "is_error": False, "detail": result}

    # ══════════ 工具：下一手牌 ══════════

    @llm_tool(
        name="card_next_hand",
        description="开始下一手德州扑克牌（移动庄家位、重新洗牌发牌）。何时用：一手牌结束后想继续打下一手时。前置：需先用 card_new_game 创建牌局且当前牌局已结束。",
        parameters={"type": "object", "properties": {}},
        timeout=30.0,
    )
    async def card_next_hand(self, **_):
        with self._lock:
            if self._game is None:
                return {"output": "当前没有牌局，请先用 card_new_game 创建。", "is_error": True, "error": "NO_GAME"}
            result = self._game.next_hand()
        state = result.get("game_state")
        return {
            "output": (f"第 {state['hand_number']} 手牌开始！\n"
                       f"当前玩家：{state['current_player']}（手牌 {state['current_player_hand']}，筹码 {state['current_player_chips']}）\n"
                       f"底池：{state['pot']}，需跟注：{state['to_call']}"),
            "is_error": False,
            "game_state": state,
        }

    # ══════════ Kards 专用工具 ══════════

    @llm_tool(
        name="kards_keyword",
        description="查询 Kards 卡牌游戏的能力关键词含义。何时用：遇到不认识的 Kards 关键词（闪击/部署/亡计/守护/伏击/冲击/重甲/烟幕/压制/钳击等）时。参数 name：关键词名称（不填则列出全部20+个关键词）。数据来自 Kards 官方 wiki。",
        parameters={"type": "object", "properties": {
            "name": {"type": "string", "description": "关键词名称，如 '闪击'。不填则列出全部"},
        }},
        timeout=30.0,
    )
    async def kards_keyword(self, *, name: str = "", **_):
        params = _.get("parameters") or {}
        n = name or params.get("name", "")
        return {"output": kards_mod.get_keyword(n), "is_error": False}

    @llm_tool(
        name="kards_nation",
        description="查询 Kards 国家阵营特色。何时用：想了解某个国家（德国/英国/苏联/美国/日本/法国/意大利/波兰/芬兰）的玩法风格和特色时。参数 name：国家名称（不填则列出全部国家）。数据来自 Kards 官方 wiki。",
        parameters={"type": "object", "properties": {
            "name": {"type": "string", "description": "国家名称，如 '德国'。不填则列出全部"},
        }},
        timeout=30.0,
    )
    async def kards_nation(self, *, name: str = "", **_):
        params = _.get("parameters") or {}
        n = name or params.get("name", "")
        return {"output": kards_mod.get_nation(n), "is_error": False}

    @llm_tool(
        name="kards_deck_check",
        description="检查 Kards 卡组是否合法（40张=1总部+39张，1主国+1盟国，国家有效性）。何时用：构筑或修改 Kards 卡组后想验证是否符合规则时。参数 deck：JSON数组，每个元素 {name, type, nation, count}，type 为 HQ/unit/order，nation 为国家名。返回是否合法、错误列表、警告、统计摘要。",
        parameters={"type": "object", "properties": {
            "deck": {"type": "string", "description": "JSON数组，如 '[{\"name\":\"德国总部\",\"type\":\"HQ\",\"nation\":\"德国\",\"count\":1},...]'"},
        }, "required": ["deck"]},
        timeout=30.0,
    )
    async def kards_deck_check(self, *, deck: str = "", **_):
        params = _.get("parameters") or {}
        deck_str = deck or params.get("deck", "")
        if not deck_str:
            return {"output": "请提供 deck 参数（JSON数组）。", "is_error": True, "error": "MISSING_DECK"}
        try:
            deck_list = json.loads(deck_str)
            if not isinstance(deck_list, list):
                raise ValueError("deck 必须是数组")
        except (json.JSONDecodeError, ValueError) as e:
            return {"output": f"deck 格式错误：{e}。应为 JSON 数组。", "is_error": True, "error": "INVALID_DECK"}

        result = kards_mod.check_deck(deck_list)
        s = result["summary"]
        lines = [
            f"卡组检查：{'✅ 合法' if result['valid'] else '❌ 不合法'}",
            f"总计 {s['total']} 张 | 总部 {s['headquarters']} | 单位 {s['units']} | 指令 {s['orders']}",
            f"主要国：{', '.join(s['main_nations']) or '（未检测到）'} | 盟国：{', '.join(s['ally_nations']) or '（无）'}",
        ]
        if result["errors"]:
            lines.append("")
            lines.append("错误：")
            for e in result["errors"]:
                lines.append(f"  ❌ {e}")
        if result["warnings"]:
            lines.append("")
            lines.append("警告：")
            for w in result["warnings"]:
                lines.append(f"  ⚠️ {w}")
        return {"output": "\n".join(lines), "is_error": False, "detail": result}
