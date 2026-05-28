import base64
import json
import re
from typing import Any

import aiohttp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, register


OVERSTATS_BASE_URL = "http://127.0.0.1:18080"
BINDINGS_KEY = "bindings"
BNET_RE = re.compile(r"^[^#\s]{1,32}#[0-9]{4,6}$")


@register("astrbot_plugin_overstats", "OpenCode", "通过 Overstats 查询守望先锋战绩", "1.0.0")
class OverstatsPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @filter.command("ow")
    async def ow(self, event: AstrMessageEvent):
        """查询守望先锋战绩。用法：/ow、/ow 绑定 Player#12345、/ow 近期对局等。"""
        args = self._extract_args(event)
        parts = args.split()
        action = parts[0] if parts else ""

        if action == "绑定":
            if len(parts) != 2:
                yield event.plain_result("用法：/ow 绑定 玩家id，例如 /ow 绑定 Player#12345")
                return

            result = await self._bind_player(event.get_sender_id(), parts[1])
            yield event.plain_result(result)
            return

        if action.lower() == "help":
            yield event.plain_result(self._help_text())
            return

        bnet_id = await self._get_user_bnet_id(event.get_sender_id())
        if not bnet_id:
            yield event.plain_result("你还没有绑定守望先锋玩家 ID，请先使用 /ow 绑定 玩家id，例如 /ow 绑定 Player#12345")
            return

        if not action:
            async for result in self._send_image_or_json(
                event,
                "/api/v2/dashen-profile/image",
                "/api/v2/dashen-profile",
                {"bnet_id": bnet_id, "include_previous_season": True, "mode": "quick"},
                timeout=35,
            ):
                yield result
            return

        if action in {"英雄云图", "云图"}:
            async for result in self._send_image_or_json(
                event,
                "/api/v2/dashen-hero-treemap/image",
                "/api/v2/dashen-hero-treemap",
                {"bnet_id": bnet_id, "include_previous_season": True, "mode": "quick"},
                timeout=45,
            ):
                yield result
            return

        if action == "近期对局":
            async for result in self._send_image_or_json(
                event,
                "/api/v2/dashen-match/image",
                "/api/v2/dashen-match",
                {"bnet_id": bnet_id, "limit": 20, "include_fight": True, "include_previous_season": True},
                timeout=45,
            ):
                yield result
            return

        if action == "对局":
            if len(parts) != 2 or not parts[1].isdigit():
                yield event.plain_result("用法：/ow 对局 对局索引，例如 /ow 对局 0")
                return

            payload = {
                "bnet_id": bnet_id,
                "index": int(parts[1]),
                "limit": 20,
                "include_fight": True,
                "include_previous_season": True,
                "show_all_heroes": True,
                "analyze": True,
            }
            async for result in self._send_replies_or_json(
                event,
                "/api/v2/dashen-match/detail/replies",
                "/api/v2/dashen-match/detail",
                payload,
                timeout=90,
            ):
                yield result
            return

        summary_scopes = {
            "今日总结": ("today", 35),
            "昨日总结": ("yesterday", 55),
            "本周总结": ("week", 120),
        }
        if action in summary_scopes:
            scope, timeout = summary_scopes[action]
            async for result in self._send_image_or_json(
                event,
                f"/api/v2/dashen-summary/{scope}/image",
                f"/api/v2/dashen-summary/{scope}",
                {"bnet_id": bnet_id, "full_id": bnet_id},
                timeout=timeout,
            ):
                yield result
            return

        yield event.plain_result(self._help_text())

    async def _bind_player(self, user_id: str, bnet_id: str) -> str:
        if not BNET_RE.fullmatch(bnet_id):
            return "玩家 ID 格式不正确，应为 Player#12345，例如 /ow 绑定 Player#12345"

        bindings = await self._get_bindings()
        users = bindings.setdefault("users", {})
        players = bindings.setdefault("players", {})

        bound_user = players.get(bnet_id)
        if bound_user and bound_user != user_id:
            return f"{bnet_id} 已被其他用户绑定，一个玩家 ID 只能绑定一个用户。"

        old_bnet_id = users.get(user_id)
        if old_bnet_id and old_bnet_id != bnet_id:
            players.pop(old_bnet_id, None)

        users[user_id] = bnet_id
        players[bnet_id] = user_id
        await self.put_kv_data(BINDINGS_KEY, bindings)
        return f"已绑定守望先锋玩家 ID：{bnet_id}"

    async def _get_user_bnet_id(self, user_id: str) -> str | None:
        bindings = await self._get_bindings()
        return bindings.get("users", {}).get(user_id)

    async def _get_bindings(self) -> dict[str, dict[str, str]]:
        data = await self.get_kv_data(BINDINGS_KEY, {"users": {}, "players": {}})
        if not isinstance(data, dict):
            return {"users": {}, "players": {}}
        users = data.get("users") if isinstance(data.get("users"), dict) else {}
        players = data.get("players") if isinstance(data.get("players"), dict) else {}
        return {"users": users, "players": players}

    async def _send_image_or_json(
        self,
        event: AstrMessageEvent,
        image_endpoint: str,
        json_endpoint: str,
        payload: dict[str, Any],
        *,
        timeout: int,
    ):
        image_bytes = await self._post_bytes(image_endpoint, payload, timeout=timeout)
        if image_bytes:
            yield event.make_result().base64_image(base64.b64encode(image_bytes).decode("ascii"))
            return

        yield event.plain_result(await self._post_json_text(json_endpoint, payload, timeout=timeout))

    async def _send_replies_or_json(
        self,
        event: AstrMessageEvent,
        replies_endpoint: str,
        json_endpoint: str,
        payload: dict[str, Any],
        *,
        timeout: int,
    ):
        data = await self._post_json(replies_endpoint, payload, timeout=timeout)
        chain = []
        if isinstance(data, dict) and data.get("ok"):
            for item in data.get("replies", []):
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "image" and item.get("base64"):
                    chain.append(Image.fromBase64(item["base64"]))
                elif item.get("type") == "text" and item.get("data"):
                    chain.append(Plain(str(item["data"])))
            if chain:
                yield event.chain_result(chain)
                return

        yield event.plain_result(await self._post_json_text(json_endpoint, payload, timeout=timeout))

    async def _post_bytes(self, endpoint: str, payload: dict[str, Any], *, timeout: int) -> bytes | None:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                async with session.post(self._url(endpoint), json=payload) as response:
                    content_type = response.headers.get("Content-Type", "")
                    if response.status == 200 and content_type.startswith("image/"):
                        return await response.read()
                    logger.warning("Overstats image request failed: %s %s", response.status, await response.text())
        except Exception as exc:
            logger.warning("Overstats image request error: %s", exc)
        return None

    async def _post_json_text(self, endpoint: str, payload: dict[str, Any], *, timeout: int) -> str:
        data = await self._post_json(endpoint, payload, timeout=timeout)
        if data is None:
            return "Overstats 请求失败，请稍后再试。"
        return "图片生成失败，已返回 JSON：\n" + json.dumps(data, ensure_ascii=False, indent=2)[:4000]

    async def _post_json(self, endpoint: str, payload: dict[str, Any], *, timeout: int) -> Any | None:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                async with session.post(self._url(endpoint), json=payload) as response:
                    content_type = response.headers.get("Content-Type", "")
                    if "application/json" in content_type:
                        return await response.json(content_type=None)
                    text = await response.text()
                    logger.warning("Overstats json request returned non-json: %s %s", response.status, text[:500])
                    return {"ok": False, "status": response.status, "body": text[:2000]}
        except Exception as exc:
            logger.warning("Overstats json request error: %s", exc)
            return None

    def _url(self, endpoint: str) -> str:
        return f"{OVERSTATS_BASE_URL}{endpoint}"

    def _extract_args(self, event: AstrMessageEvent) -> str:
        message = re.sub(r"\s+", " ", event.get_message_str().strip())
        return message[2:].strip() if message == "ow" or message.startswith("ow ") else ""

    def _help_text(self) -> str:
        return (
            "支持的命令：\n"
            "/ow help （查看帮助）\n"
            "/ow 绑定 玩家id (绑定玩家id)\n"
            "/ow (查看玩家资料)\n"
            "/ow 英雄云图 （查看英雄云图） 或 /ow 云图\n"
            "/ow 近期对局 （查看最近对局）\n"
            "/ow 对局 对局索引\n"
            "/ow 今日总结\n"
            "/ow 昨日总结\n"
            "/ow 本周总结（速度较慢）"
        )

    async def terminate(self):
        pass
