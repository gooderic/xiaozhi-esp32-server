import os
import re
import json
import random
import asyncio
import aiohttp
from core.providers.tts.dto.dto import TTSMessageDTO, SentenceType, ContentType
from plugins_func.register import register_function, ToolType, ActionResponse, Action
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__

# go-music-api 聚合音源（本机 docker 容器 xiaozhi-music-api，仅绑定 127.0.0.1）
MUSIC_API = os.environ.get("MUSIC_API_BASE", "http://127.0.0.1:8080")
# 缓存目录：xiaozhi-server/tmp/online_music
CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "tmp",
    "online_music",
)
MAX_FILE_MB = 50  # 单首歌大小上限
MAX_CACHE_FILES = 200  # 缓存文件数上限，超出删最旧的

play_online_music_function_desc = {
    "type": "function",
    "function": {
        "name": "play_online_music",
        "description": (
            "在线搜索并播放任意歌曲（曲库覆盖全网，不受服务器本地音乐文件限制）。"
            "当用户点名要听某首歌、某位歌手的歌，或说'播放音乐/来首歌'时，优先调用本工具。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "song_name": {
                    "type": "string",
                    "description": (
                        "歌曲名称。用户没指定具体歌名时传'random'。"
                        " 示例: 用户:播放周杰伦的晴天 -> song_name=晴天, artist=周杰伦"
                    ),
                },
                "artist": {
                    "type": "string",
                    "description": "歌手名称，用户未提及可不传",
                },
            },
            "required": ["song_name"],
        },
    },
}


@register_function("play_online_music", play_online_music_function_desc, ToolType.SYSTEM_CTL)
async def play_online_music(conn: "ConnectionHandler", song_name: str, artist: str = ""):
    try:
        if not song_name or song_name == "random":
            song_name = random.choice(
                ["热门华语金曲", "经典老歌", "抖音热门歌曲", "流行歌曲", "轻音乐纯音乐"]
            )
            artist = ""
        conn.logger.bind(tag=TAG).info(f"在线播放请求: {song_name} - {artist}")

        song = await _search_song(song_name, artist)
        if song is None:
            return ActionResponse(
                action=Action.RESPONSE,
                result=f"未找到歌曲: {song_name}",
                response=f"抱歉，没有搜到《{song_name}》",
            )

        url, final_song = await _resolve_url(song)
        if not url:
            return ActionResponse(
                action=Action.RESPONSE,
                result=f"无法获取播放链接: {song_name}",
                response=f"《{final_song.get('name', song_name)}》暂时拿不到播放链接，换个平台试试或换首歌吧",
            )

        music_path = await _download(url, final_song)
        if not music_path:
            return ActionResponse(
                action=Action.RESPONSE,
                result="下载失败",
                response="歌曲下载失败了，请稍后再试",
            )

        text = f"正在为您播放，《{final_song.get('name', song_name)}》"
        await _push_music_to_tts(conn, text, music_path)
        return ActionResponse(action=Action.RECORD, result="指令已接收", response=text)
    except asyncio.TimeoutError:
        conn.logger.bind(tag=TAG).error(f"在线播放超时: {song_name}")
        return ActionResponse(
            action=Action.RESPONSE, result="timeout", response="网络有点慢，获取歌曲超时了"
        )
    except Exception as e:  # noqa: BLE001
        conn.logger.bind(tag=TAG).error(f"在线播放出错: {e}")
        return ActionResponse(
            action=Action.RESPONSE, result=str(e), response="播放在线音乐时出错了"
        )


async def _api_get(path, params=None, timeout=15):
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{MUSIC_API}{path}",
            params=params,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            return await resp.json(content_type=None)


def _song_query_params(song):
    """把 go-music-api 的歌曲对象转成 /music/url 查询参数"""
    params = {}
    for key in ("id", "source", "name", "artist", "album", "duration"):
        if song.get(key) not in (None, ""):
            params[key] = song[key]
    if isinstance(song.get("extra"), dict) and song["extra"]:
        params["extra"] = json.dumps(song["extra"], ensure_ascii=False)
    return params


async def _search_song(song_name, artist):
    q = f"{song_name} {artist}".strip()
    try:
        data = await _api_get("/api/v1/music/search", {"q": q, "type": "song"})
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"搜索接口失败: {e}") from e
    body = data.get("data") or data
    songs = body.get("songs") or body.get("list") or []
    if not songs:
        return None
    # 优先歌名包含关键词的结果，其次歌手匹配
    for s in songs:
        if song_name and song_name in (s.get("name") or ""):
            return s
    for s in songs:
        if artist and artist in (s.get("artist") or ""):
            return s
    return songs[0]


async def _resolve_url(song):
    """取播放链接；直连失败时用 switch 换平台再取"""
    try:
        data = await _api_get("/api/v1/music/url", _song_query_params(song))
        url = (data.get("data") or {}).get("url")
        if url:
            return url, song
    except Exception:  # noqa: BLE001  # 解析失败走 switch
        pass

    switch_params = {}
    for key in ("name", "artist", "source", "duration"):
        if song.get(key) not in (None, ""):
            switch_params[key] = song[key]
    try:
        data = await _api_get("/api/v1/music/switch", switch_params)
        body = data.get("data") or {}
        new_song = dict(song)
        for key in ("id", "source", "name", "artist", "duration", "extra"):
            if body.get(key) not in (None, ""):
                new_song[key] = body[key]
        data2 = await _api_get("/api/v1/music/url", _song_query_params(new_song))
        url = (data2.get("data") or {}).get("url")
        return url, new_song
    except Exception:  # noqa: BLE001
        return None, song


def _safe_filename(name):
    return re.sub(r'[\\/:*?"<>|\s]+', "_", str(name))[:80] or "song"


async def _download(url, song):
    os.makedirs(CACHE_DIR, exist_ok=True)
    fname = _safe_filename(f"{song.get('name', 'song')}-{song.get('artist', '')}-{song.get('source', '')}") + ".mp3"
    path = os.path.join(CACHE_DIR, fname)
    if os.path.exists(path) and os.path.getsize(path) > 1024:
        return path  # 命中缓存

    try:
        timeout = aiohttp.ClientTimeout(total=None, connect=10, sock_read=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                total = 0
                with open(path + ".part", "wb") as f:
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        total += len(chunk)
                        if total > MAX_FILE_MB * 1024 * 1024:
                            f.close()
                            os.remove(path + ".part")
                            return None
                        f.write(chunk)
        if total < 1024:  # 太小多半是错误页
            os.remove(path + ".part")
            return None
        os.rename(path + ".part", path)
        _evict_cache()
        return path
    except Exception:  # noqa: BLE001
        if os.path.exists(path + ".part"):
            try:
                os.remove(path + ".part")
            except OSError:
                pass
        return None


def _evict_cache():
    try:
        files = [
            os.path.join(CACHE_DIR, f)
            for f in os.listdir(CACHE_DIR)
            if not f.endswith(".part")
        ]
        if len(files) <= MAX_CACHE_FILES:
            return
        files.sort(key=lambda p: os.path.getmtime(p))
        for p in files[: len(files) - MAX_CACHE_FILES]:
            try:
                os.remove(p)
            except OSError:
                pass
    except OSError:
        pass


async def _push_music_to_tts(conn: "ConnectionHandler", text, music_path):
    """与内置 play_music 相同的推流方式：一段 TTS 报幕 + 音乐文件"""
    conn.tts.store_tts_text(conn.sentence_id, text)
    if conn.intent_type == "intent_llm":
        conn.tts.tts_text_queue.put(
            TTSMessageDTO(
                sentence_id=conn.sentence_id,
                sentence_type=SentenceType.FIRST,
                content_type=ContentType.ACTION,
            )
        )
    conn.tts.tts_text_queue.put(
        TTSMessageDTO(
            sentence_id=conn.sentence_id,
            sentence_type=SentenceType.MIDDLE,
            content_type=ContentType.TEXT,
            content_detail=text,
        )
    )
    conn.tts.tts_text_queue.put(
        TTSMessageDTO(
            sentence_id=conn.sentence_id,
            sentence_type=SentenceType.MIDDLE,
            content_type=ContentType.FILE,
            content_file=music_path,
        )
    )
    if conn.intent_type == "intent_llm":
        conn.tts.tts_text_queue.put(
            TTSMessageDTO(
                sentence_id=conn.sentence_id,
                sentence_type=SentenceType.LAST,
                content_type=ContentType.ACTION,
            )
        )
