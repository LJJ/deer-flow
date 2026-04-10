"""拍摄系统数据模型 — FilmBrief 和 SegmentPlan"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MaterialSignals:
    """素材信号 — 描述 FilmBrief 覆盖时间段的统计特征"""

    event_count: int = 0
    event_types: list[str] = field(default_factory=list)
    character_count: int = 0
    location_changes: int = 0
    dialogue_turns: int = 0
    time_span_minutes: float = 0


@dataclass
class WorldEvent:
    """筛选出的世界事件原文"""

    ts: str
    type: str
    character: str
    content: str
    location: str = ""
    target: str = ""


@dataclass
class FilmBrief:
    """拍摄简报 — Scene Curator 的输出，Cinematographer 的输入"""

    time_range_start: str
    time_range_end: str
    characters: list[str]
    location_summary: str
    mood: str
    material_signals: MaterialSignals
    selected_events: list[WorldEvent]
    narrative_summary: str


@dataclass
class CharacterInSegment:
    """段内角色 — 含着装信息（NPC 无 outfit_item_id 和 element）"""

    character_id: str
    outfit_item_id: str = ""
    is_npc: bool = False
    display_name: str = ""  # NPC 中文名（如"陈昊"），主角留空走 CHARACTER_DISPLAY_NAMES


@dataclass
class Shot:
    """段内单个镜头"""

    shot_index: int
    scale: str  # 远景/全景/中景/近景/特写/大特写
    camera_movement: str  # 固定/推/拉/横移/跟拍/升降
    duration_seconds: float
    shot_prompt: str  # 该镜头的画面描述


@dataclass
class Segment:
    """视频段 — SegmentPlan 的基本单元"""

    segment_index: int
    scene_description: str
    duration_seconds: float
    aspect_ratio: str  # "16:9" or "9:16"
    transition_to_next: str  # "first_frame" / "scene_reference" / "hard_cut"
    characters: list[CharacterInSegment]
    shots: list[Shot]
    perspective: str = "first_person"  # "first_person"（公子在场）或 "third_person"（公子不在场）
    prompt: str = ""  # Cinematographer 直接输出的整段连贯叙事，供 KlingAI 使用


@dataclass
class SegmentPlan:
    """拍摄方案 — Cinematographer 的输出，执行管线的输入"""

    segments: list[Segment]

    @property
    def total_duration(self) -> float:
        return sum(s.duration_seconds for s in self.segments)

    @property
    def total_segments(self) -> int:
        return len(self.segments)

    def validate_constraints(self, max_segments: int = 6, max_duration: float = 90.0) -> list[str]:
        """检查硬约束，返回违规列表"""
        errors = []
        if self.total_segments > max_segments:
            errors.append(f"总段数 {self.total_segments} 超过上限 {max_segments}")
        if self.total_duration > max_duration:
            errors.append(f"总时长 {self.total_duration:.1f}s 超过上限 {max_duration}s")
        for seg in self.segments:
            if seg.duration_seconds < 5 or seg.duration_seconds > 15:
                errors.append(f"段 {seg.segment_index} 时长 {seg.duration_seconds}s 不在 5-10s 最佳区间")
            # 检查单段单角色单 element
            char_ids = [c.character_id for c in seg.characters]
            if len(char_ids) != len(set(char_ids)):
                errors.append(f"段 {seg.segment_index} 存在重复角色")
            # shots 非空
            if not seg.shots:
                errors.append(f"段 {seg.segment_index} 没有镜头（shots 为空）")
            else:
                shot_total = sum(s.duration_seconds for s in seg.shots)
                if abs(shot_total - seg.duration_seconds) > 1.0:
                    errors.append(f"段 {seg.segment_index} 镜头时长之和 {shot_total:.1f}s ≠ 段时长 {seg.duration_seconds}s")
        return errors
