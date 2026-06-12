"""质量扫描服务 - 复用 emby-pulse insight 模块的逻辑"""

from __future__ import annotations
import asyncio
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional

from app.services.emby import EmbyClient
from app.database import save_quality_cache, get_ignore_ids


# ─── 质量权重定义 ───

RESOLUTION_WEIGHTS = {
    "7680x4320": 100,  # 8K
    "3840x2160": 90,   # 4K
    "3840x1608": 88,
    "3840x1604": 88,
    "3840x1600": 88,
    "2560x1440": 75,   # 2K
    "2560x1080": 70,
    "1920x1080": 60,   # 1080p
    "1920x1040": 55,
    "1920x800":  50,
    "1280x720":  35,   # 720p
    "1280x534":  30,
    "720x480":   15,   # DVD
    "720x576":   15,
    "640x480":   10,   # SD
}

CODEC_WEIGHTS = {
    "av1":   90,
    "hevc":  80,
    "h265":  80,
    "x265":  80,
    "h264":  50,
    "x264":  50,
    "mpeg4": 20,
    "mpeg2": 10,
    "vc1":   15,
}

HDR_WEIGHTS = {
    "DolbyVision": 95,
    "Dolby Vision": 95,
    "HDR10":       80,
    "HDR":         75,
    "SDR":         40,
}

VIDEO_BITRATE_WEIGHTS = {
    "high":  30,   # > 40 Mbps
    "medium": 20,  # 10-40 Mbps
    "low":    5,   # < 10 Mbps
}


def parse_filename_resolution(path: str) -> tuple[int,int] | None:
    """从文件名解析分辨率，作为 Emby MediaStreams 的回退"""
    if not path:
        return None
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    patterns = [
        r'(?:2160|4k|uhd|4320p?)',    # 4K / UHD → 3840x2160
        r'(?:1080[pi]?|fhd)',          # 1080p / FHD → 1920x1080
        r'(?:720[pi]?)',               # 720p → 1280x720
        r'(?:480[pi]?)',               # 480p → 720x480
        r'(?:576[pi]?)',               # 576p → 720x576
        r'(?:360[pi]?)',               # 360p → 640x360
    ]
    name_lower = name.lower()
    for pat in patterns:
        m = re.search(pat, name_lower)
        if m:
            match = m.group(0)
            if any(k in match for k in ["2160", "4k", "uhd", "4320"]):
                return (3840, 2160)
            if any(k in match for k in ["1080", "fhd"]):
                return (1920, 1080)
            if "720" in match:
                return (1280, 720)
            if "480" in match:
                return (720, 480)
            if "576" in match:
                return (720, 576)
            if "360" in match:
                return (640, 360)
    return None


def parse_filename_year(path: str) -> int | None:
    """从路径中解析 1900-2099 年份，作为 Emby 列表缺年份时的回退。"""
    if not path:
        return None
    m = re.search(r"(?:^|[^\d])((?:19|20)\d{2})(?:[^\d]|$)", path)
    return int(m.group(1)) if m else None


def get_effective_resolution(item: dict, video_stream: dict) -> tuple[int,int]:
    """获取分辨率：Emby 数据优先，文件名回退"""
    width = video_stream.get("Width", 0) or 0
    height = video_stream.get("Height", 0) or 0
    # 如果 Emby 返回的分辨率有效（≥480），直接使用
    if height >= 480 and width > 0:
        return (width, height)
    # 文件名回退（Emby 无有效分辨率时）
    parsed = parse_filename_resolution(item.get("Path", ""))
    if parsed:
        return parsed
    # 兜底
    return (0, 0)


def detect_resolution_anomaly(item: dict, video_stream: dict) -> bool:
    """检测分辨率是否异常：Emby 值与文件名标记严重不一致时标记"""
    width = video_stream.get("Width", 0) or 0
    height = video_stream.get("Height", 0) or 0

    # 从文件名解析分辨率
    parsed = parse_filename_resolution(item.get("Path", ""))
    if not parsed:
        return False  # 文件名无标记，无法判断

    fname_w, fname_h = parsed

    # 无有效 Emby 高度 → 无法比对
    if height <= 0:
        return False

    # 核心规则：文件名明确标 1080p/4K，但 Emby 高度严重偏低
    # 1080p 文件名但 Emby < 800px → 异常
    if fname_h >= 1080 and height < fname_h * 0.75:
        return True

    # 非标准宽高组合（如 1920x720 非 16:9 标准）
    if width > 0 and height > 0:
        # 标准 16:9 高度表：同一宽度下常见高度
        std_heights = {
            3840: [2160, 1608, 1604, 1600],  # 4K 及 scope 变体
            1920: [1080, 1040, 800, 816],    # 1080p 及 scope 变体
            1280: [720, 534, 536],           # 720p 及变体
        }
        if width in std_heights and height not in std_heights[width]:
            # 但还要排除 720p 文件名标 1080p 这种正常情况
            # 先看文件名标记的分辨率是否与 Emby 对应
            if fname_h >= 1080 and height < 800:
                return True  # 文件名 1080p/4K 但实际高度诡异

    return False


def calculate_quality_score(item: dict) -> int:
    """计算单条媒体的质量评分 (0-100)，分数尽量分散以覆盖全范围"""
    score = 0
    sources = item.get("MediaSources") or []
    media_source = sources[0] if sources else {}
    streams = media_source.get("MediaStreams") or []

    # 找视频流
    video_stream = None
    for s in streams:
        if s.get("Type") == "Video":
            video_stream = s
            break

    if not video_stream:
        return 30  # 默认中等

    # 1. 分辨率评分 (权重 35%, 满分约 35)
    eff_w, eff_h = get_effective_resolution(item, video_stream)
    res_key = f"{eff_w}x{eff_h}"
    res_score = RESOLUTION_WEIGHTS.get(res_key, 0)
    if res_score == 0 and eff_h > 0:
        # 近似计算
        if eff_h >= 2160:
            res_score = 90
        elif eff_h >= 1440:
            res_score = 75
        elif eff_h >= 1080:
            res_score = 60
        elif eff_h >= 720:
            res_score = 35
        else:
            res_score = 10
    score += res_score * 0.35  # 分辨率权重 35%

    # 2. 编码格式评分 (权重 30%, 满分约 27)
    codec = (video_stream.get("Codec") or "").lower()
    codec_score = 0
    for k, v in CODEC_WEIGHTS.items():
        if k in codec:
            codec_score = v
            break
    score += codec_score * 0.30  # 编码权重 30%

    # 3. HDR 评分 (权重 20%, 满分约 19)
    video_range = (video_stream.get("VideoRange") or "").lower()
    hdr_score = 0
    if "dolby" in video_range or "dovision" in video_range:
        hdr_score = 95
    elif "hdr" in video_range:
        hdr_score = 75
    else:
        hdr_score = 40
    score += hdr_score * 0.20  # HDR 权重 20%

    # 4. 码率评分 (权重 10%, 满分 10) — 更细粒度
    bitrate = media_source.get("Bitrate", 0)
    if bitrate > 80_000_000:
        br_score = 100
    elif bitrate > 40_000_000:
        br_score = 80
    elif bitrate > 20_000_000:
        br_score = 60
    elif bitrate > 10_000_000:
        br_score = 40
    elif bitrate > 5_000_000:
        br_score = 20
    else:
        br_score = 5
    score += br_score * 0.10  # 码率权重 10%

    return int(min(score, 100))


def classify_resolution(item: dict, video_stream: dict) -> str:
    """分类分辨率（文件名回退）"""
    _, height = get_effective_resolution(item, video_stream)
    if height >= 2160:
        return "4k"
    elif height >= 1080:
        return "1080p"
    elif height >= 720:
        return "720p"
    elif height >= 480:
        return "sd"
    return "sd"


def classify_codec(video_stream: dict) -> str:
    """分类编码"""
    codec = (video_stream.get("Codec") or "").lower()
    if "av1" in codec:
        return "av1"
    elif "hevc" in codec or "h265" in codec or "x265" in codec:
        return "hevc"
    elif "h264" in codec or "x264" in codec:
        return "h264"
    return "other_codec"


def classify_hdr(video_stream: dict) -> str:
    """分类 HDR 类型"""
    vr = (video_stream.get("VideoRange") or "").lower()
    if "dolby" in vr:
        return "dolby_vision"
    elif "hdr" in vr:
        return "hdr10"
    return "sdr"


class QualityScanner:
    """质量扫描器"""

    def __init__(self, emby: EmbyClient):
        self.emby = emby
        self._is_scanning = False
        self._progress = 0
        self._current_item = ""
        self._total = 0
        self._scanned = 0

    @property
    def progress(self) -> dict:
        return {
            "is_scanning": self._is_scanning,
            "progress": self._progress if self._total > 0 else 0,
            "current_item": self._current_item,
            "total_count": self._total,
            "scanned_count": self._scanned,
        }

    async def scan(self, excluded_libraries: list[str] = None, max_items: int = 99999) -> dict:
        """执行质量扫描，返回汇总"""
        self._is_scanning = True
        self._progress = 0
        self._scanned = 0
        self._excluded_count = 0
        print("[Scan] 开始质量扫描")

        try:
            ignored_ids = await get_ignore_ids()

            # ── 1. 获取所有媒体库，筛选电影/电视剧类型 ──
            all_libraries = await self.emby.get_libraries()
            media_libs = [
                lib for lib in all_libraries
                if lib.get("CollectionType") in ("movies", "tvshows")
            ]
            lib_map = {lib.get("ItemId", ""): lib.get("Name", "") for lib in all_libraries}

            # ── 2. 确定排除的媒体库（仅按 ItemId 匹配）──
            excluded_ids = set()
            excluded_names = set()
            for eid in (excluded_libraries or []):
                eid = eid.strip()
                if not eid:
                    continue
                found = False
                for lib in media_libs:
                    lib_id = lib.get("ItemId", "")
                    if lib_id == eid:
                        excluded_ids.add(lib_id)
                        excluded_names.add(lib.get("Name", ""))
                        print(f"[Scan] 排除媒体库: {lib.get('Name', '')} (ID={lib_id})")
                        found = True
                        break
                if not found:
                    print(f"[Scan] ⚠ 未匹配到可扫描媒体库: {eid}（忽略）")

            # ── 3. 确定要扫描的媒体库 ──
            included_libs = [lib for lib in media_libs if lib.get("ItemId", "") not in excluded_ids]

            if not included_libs:
                print("[Scan] ⚠ 没有需要扫描的电影/电视剧媒体库")
                return {"total_count": 0, "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "resolution_dist": {}, "codec_dist": {}, "hdr_dist": {}, "anomaly_count": 0}

            print(f"[Scan] 将扫描 {len(included_libs)} 个媒体库: {[lib.get('Name','') for lib in included_libs]}")
            if excluded_names:
                print(f"[Scan] 已排除 {len(excluded_names)} 个媒体库: {sorted(excluded_names)}")

            # ── 4. 按库逐个扫描（Emby 只支持单库 ParentId 过滤）──
            all_items = []
            limit = 200
            total_estimated = 0
            lib_totals = {}

            for lib in included_libs:
                lib_id = lib.get("ItemId", "")
                lib_type = lib.get("CollectionType", "")
                include_types = "Series" if lib_type == "tvshows" else "Movie"

                # 获取该库总条目数
                first_page = await self.emby.get_items(
                    include_types=include_types,
                    fields="MediaSources,Path,MediaStreams,ProviderIds,ProductionYear,PremiereDate,DateCreated",
                    start_index=0,
                    limit=1,
                    parent_id=lib_id,
                )
                lib_total = (first_page or {}).get("TotalRecordCount", 0) or 0
                lib_totals[lib_id] = min(lib_total, max_items)
                total_estimated += lib_totals[lib_id]

            self._total = total_estimated

            for lib in included_libs:
                lib_id = lib.get("ItemId", "")
                lib_name = lib.get("Name", "")
                lib_type = lib.get("CollectionType", "")
                lib_total = lib_totals[lib_id]
                lib_scanned = 0

                if lib_type == "tvshows":
                    series_items: list[dict] = []
                    start = 0
                    while start < lib_total and len(series_items) < max_items:
                        page = await self.emby.get_items(
                            include_types="Series",
                            fields="Path,ProviderIds,ProductionYear,PremiereDate,RecursiveItemCount,ChildCount",
                            start_index=start,
                            limit=limit,
                            parent_id=lib_id,
                        )
                        items = (page or {}).get("Items", [])
                        if not items:
                            break
                        series_items.extend(items)
                        start += limit

                    semaphore = asyncio.Semaphore(8)

                    async def build_series_item(series: dict) -> dict | None:
                        sid = series.get("Id", "")
                        episodes = []
                        ep_start = 0
                        ep_total = 1
                        async with semaphore:
                            while ep_start < ep_total:
                                page = await self.emby.get_items(
                                    include_types="Episode",
                                    fields="MediaSources,Path,MediaStreams,ProviderIds,ProductionYear,PremiereDate,DateCreated,SeriesId,SeriesName,ParentIndexNumber,IndexNumber",
                                    start_index=ep_start,
                                    limit=300,
                                    parent_id=sid,
                                )
                                batch = (page or {}).get("Items", [])
                                ep_total = (page or {}).get("TotalRecordCount", 0) or 0
                                if not batch:
                                    break
                                episodes.extend(batch)
                                ep_start += 300
                        if not episodes:
                            return None
                        representative = min(episodes, key=calculate_quality_score)
                        return {
                            **representative,
                            "Id": sid,
                            "Name": series.get("Name", "") or representative.get("SeriesName", ""),
                            "Type": "Series",
                            "ProviderIds": series.get("ProviderIds") or {},
                            "ProductionYear": series.get("ProductionYear") or parse_filename_year(series.get("Path", "")),
                            "PremiereDate": series.get("PremiereDate", ""),
                            "Path": series.get("Path", "") or representative.get("Path", ""),
                            "_library_id": lib_id,
                            "_library_name": lib_name,
                            "_episode_count": len(episodes),
                            "_representative_episode": {
                                "id": representative.get("Id", ""),
                                "season": representative.get("ParentIndexNumber"),
                                "episode": representative.get("IndexNumber"),
                                "name": representative.get("Name", ""),
                            } if representative else {},
                        }

                    batch_size = 48
                    for batch_start in range(0, len(series_items), batch_size):
                        batch = series_items[batch_start:batch_start + batch_size]
                        if not batch:
                            continue
                        self._current_item = f"{lib_name} / {batch[0].get('Name', '')} 等 {len(batch)} 部"
                        built_items = await asyncio.gather(*(build_series_item(series) for series in batch))
                        for synthetic in built_items:
                            lib_scanned += 1
                            self._scanned += 1
                            if synthetic:
                                all_items.append(synthetic)
                            self._progress = min(99, int(self._scanned / self._total * 100) if self._total else 0)
                        if self._progress % 20 == 0 and self._scanned > 0:
                            print(f"[Scan] [{lib_name}] 进度 {self._progress}% ({self._scanned}/{self._total})")
                        if len(all_items) >= max_items:
                            break
                else:
                    start = 0
                    while start < lib_total and len(all_items) < max_items:
                        page = await self.emby.get_items(
                            include_types="Movie",
                            fields="MediaSources,Path,MediaStreams,ProviderIds,ProductionYear,PremiereDate,DateCreated",
                            start_index=start,
                            limit=limit,
                            parent_id=lib_id,
                        )
                        items = (page or {}).get("Items", [])
                        if not items:
                            break
                        for item in items:
                            item["_library_id"] = lib_id
                            item["_library_name"] = lib_name
                        lib_scanned += len(items)
                        all_items.extend(items)
                        self._scanned += len(items)
                        self._progress = min(99, int(self._scanned / self._total * 100) if self._total else 0)
                        if self._progress % 20 == 0 and self._scanned > 0:
                            print(f"[Scan] [{lib_name}] 进度 {self._progress}% ({self._scanned}/{self._total})")
                        start += limit

                print(f"[Scan] [{lib_name}] 完成，共 {lib_scanned} 条")

            print(f"[Scan] 全部媒体库扫描完成，累计 {len(all_items)} 条")

            # ── 5. 分析质量 ──
            resolution_dist = {"4k": 0, "1080p": 0, "720p": 0, "sd": 0}
            codec_dist = {"hevc": 0, "h264": 0, "av1": 0, "other_codec": 0}
            hdr_dist = {"dolby_vision": 0, "hdr10": 0, "sdr": 0}

            result_items = []
            for item in all_items:
                emby_id = item.get("Id", "")
                if emby_id in ignored_ids:
                    continue

                sources = item.get("MediaSources") or []
                ms = sources[0] if sources else {}
                streams = ms.get("MediaStreams") or []

                video_stream = None
                for s in streams:
                    if s.get("Type") == "Video":
                        video_stream = s
                        break

                lib_id = item.get("LibraryId", "") or item.get("_library_id", "") or ""
                lib_name = lib_map.get(lib_id, "") or item.get("_library_name", "")
                item_type = item.get("Type", "Movie") or "Movie"
                provider_ids = item.get("ProviderIds") or {}
                year = item.get("ProductionYear") or parse_filename_year(item.get("Path", ""))

                if video_stream:
                    eff_w, eff_h = get_effective_resolution(item, video_stream)
                    is_anomaly = detect_resolution_anomaly(item, video_stream)
                    quality_item = {
                        "emby_id": emby_id,
                        "name": item.get("Name", ""),
                        "year": year,
                        "type": item_type,
                        "provider_ids": provider_ids,
                        "tmdb_id": provider_ids.get("Tmdb", ""),
                        "imdb_id": provider_ids.get("Imdb", ""),
                        "resolution": f"{eff_w}x{eff_h}",
                        "video_codec": video_stream.get("Codec", ""),
                        "video_range": video_stream.get("VideoRange", ""),
                        "path": item.get("Path", ""),
                        "library_id": lib_id,
                        "library_name": lib_name,
                        "quality_score": calculate_quality_score(item),
                        "size_bytes": ms.get("Size", 0),
                        "is_anomaly": is_anomaly,
                        "episode_count": item.get("_episode_count", 0),
                        "representative_episode": item.get("_representative_episode", {}),
                    }
                else:
                    eff_w, eff_h = parse_filename_resolution(item.get("Path", "")) or (0, 0)
                    quality_item = {
                        "emby_id": emby_id,
                        "name": item.get("Name", ""),
                        "year": year,
                        "type": item_type,
                        "provider_ids": provider_ids,
                        "tmdb_id": provider_ids.get("Tmdb", ""),
                        "imdb_id": provider_ids.get("Imdb", ""),
                        "resolution": f"{eff_w}x{eff_h}",
                        "video_codec": "",
                        "video_range": "",
                        "path": item.get("Path", ""),
                        "library_id": lib_id,
                        "library_name": lib_name,
                        "quality_score": 30 if eff_h == 0 else calculate_quality_score(item),
                        "size_bytes": ms.get("Size", 0),
                        "is_anomaly": False,
                        "episode_count": item.get("_episode_count", 0),
                        "representative_episode": item.get("_representative_episode", {}),
                    }
                result_items.append(quality_item)

                if video_stream:
                    res_cat = classify_resolution(item, video_stream)
                    codec_cat = classify_codec(video_stream)
                    hdr_cat = classify_hdr(video_stream)
                else:
                    res_cat = "sd"
                    if eff_h >= 2160: res_cat = "4k"
                    elif eff_h >= 1080: res_cat = "1080p"
                    elif eff_h >= 720: res_cat = "720p"
                    codec_cat = "other_codec"
                    hdr_cat = "sdr"
                resolution_dist[res_cat] = resolution_dist.get(res_cat, 0) + 1
                codec_dist[codec_cat] = codec_dist.get(codec_cat, 0) + 1
                hdr_dist[hdr_cat] = hdr_dist.get(hdr_cat, 0) + 1

            anomaly_count = sum(1 for it in result_items if it.get("is_anomaly"))
            summary = {
                "total_count": len(result_items),
                "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "resolution_dist": resolution_dist,
                "codec_dist": codec_dist,
                "hdr_dist": hdr_dist,
                "anomaly_count": anomaly_count,
            }

            await save_quality_cache(result_items, summary)
            excluded_names_str = ", ".join(sorted(excluded_names))
            print(f"[Scan] 扫描完成！共 {len(result_items)} 条低质量条目，扫描 {self._total} 条")
            if excluded_names:
                print(f"[Scan] 排除媒体库 [{excluded_names_str}] 共 {self._excluded_count} 条")
            if anomaly_count:
                print(f"[Scan] 异常分辨率标记 {anomaly_count} 条")
            return summary

        finally:
            self._is_scanning = False
            self._progress = 100

    async def get_items(self, min_score: int = 0, max_score: int = 60,
                        library_id: str = "", resolution: str = "",
                        video_codec: str = "", video_range: str = "",
                        anomaly: str = "",
                        sort_by: str = "quality_score",
                        sort_order: str = "asc", page: int = 1, page_size: int = 50) -> dict:
        """获取质量扫描结果"""
        import json
        from app.database import load_quality_cache

        items, summary, _ = await load_quality_cache()
        if not items:
            return {"items": [], "summary": {}, "total": 0, "page": page, "page_size": page_size}

        # 过滤
        filtered = []
        for item in items:
            score = item.get("quality_score", 0)
            if score < min_score or score > max_score:
                continue
            if library_id and item.get("library_id") != library_id:
                continue
            if resolution:
                item_res = (item.get("resolution") or "").lower()
                if resolution == "4k":
                    if not any(k in item_res for k in ["4k", "2160", "uhd"]):
                        continue
                elif resolution == "1080":
                    if not any(k in item_res for k in ["1080", "hd"]):
                        continue
                elif resolution == "720":
                    if "720" not in item_res:
                        continue
                elif resolution == "other":
                    if any(k in item_res for k in ["4k", "2160", "uhd", "1080", "hd", "720"]):
                        continue
            if video_codec:
                item_codec = (item.get("video_codec") or "").lower()
                if video_codec == "hevc":
                    if "265" not in item_codec and "hevc" not in item_codec:
                        continue
                elif video_codec == "avc":
                    if "264" not in item_codec and "avc" not in item_codec and "h264" not in item_codec:
                        continue
                elif video_codec == "av1":
                    if "av1" not in item_codec:
                        continue
                elif video_codec == "vc1":
                    if "vc-1" not in item_codec and "vc1" not in item_codec:
                        continue
            if video_range:
                item_range = (item.get("video_range") or "").lower()
                if video_range == "sdr":
                    if item_range not in ["sdr", "", "8"]:
                        continue
                elif video_range == "hdr":
                    if "hdr" not in item_range or "dolby" in item_range or "dv" in item_range:
                        continue
                elif video_range == "dolby_vision":
                    if "dolby" not in item_range and "dv" not in item_range:
                        continue
                elif video_range == "hdr10":
                    if "hdr10" not in item_range and "hdr10+" not in item_range:
                        continue
                elif video_range == "hdr10plus":
                    if "hdr10+" not in item_range:
                        continue
            if anomaly:
                item_anomaly = item.get("is_anomaly", False)
                if anomaly == "yes" and not item_anomaly:
                    continue
                if anomaly == "no" and item_anomaly:
                    continue
            filtered.append(item)

        # 排序
        reverse = sort_order == "desc"
        filtered.sort(key=lambda x: x.get(sort_by, 0) or 0, reverse=reverse)

        # 分页
        total = len(filtered)
        start = (page - 1) * page_size
        end = start + page_size
        paged = filtered[start:end]

        return {
            "items": paged,
            "summary": summary,
            "total": total,
            "page": page,
            "page_size": page_size,
        }
