---
name: kling-constraints
description: KlingAI 视频生成约束、段间过渡策略、element 规则
---

# KlingAI 约束

## 核心原则：每段尽量长，段数尽量少
KlingAI **单段视频内部**角色一致性很好，**段间**才有一致性风险（不同次生成）。
所以：每段尽量接近上限（10-15 秒），用更少的段数覆盖叙事。
镜头切换在段内用多个 shot 描述实现（KlingAI 原生支持段内多镜头），不需要为了切镜头而切段。

## 硬约束
- 单段最长 15 秒，推荐 10-15 秒
- 总段数 ≤ 6（越少越好，减少段间一致性损失）
- 总时长 ≤ 60 秒
- 单段内一个角色只能有一个 outfit_item_id（换装必须切段）
- 同次拍摄内 aspect_ratio 必须统一，所有段用同一个比例
- 横屏 16:9：多人互动、环境展示、空间叙事（餐厅、客厅、街道）
- 竖屏 9:16：单人特写、情绪聚焦、手机观看优先的内容（独白、自拍感、日常 vlog 感）
- 判断标准：场景是否需要展示空间关系和人物位置 → 横屏；场景聚焦一个人的表情和情绪 → 竖屏

## 段间过渡策略

**默认用 hard_cut。** 每段用全新镜头开篇，干净利落。观众习惯剪辑跳切，新镜头反而避免了两段之间动作节奏不匹配的割裂感。

- **hard_cut（默认）**：新场景、换节奏、换情绪、换地点、换装。大多数情况都该用这个
- **first_frame**：**仅用于**同一个连续动作的精确续写（人物还在走、手还在伸、同一个姿势的下一秒）。如果前后两段的动作、节奏、构图有任何变化，不要用 first_frame——它会让 KlingAI 在开头几秒"挣扎着"过渡，反而更割裂
- **scene_reference**：同一地点但时间跳跃，用上一段的环境作参考但人物自由重新构图

判断标准：如果你犹豫该用 first_frame 还是 hard_cut，**用 hard_cut**。

## prompt 编写要点
- 用中文描述画面内容
- 角色名直接用中文名（代码层自动替换为 element 标记）
- 不要描述衣服外观（element 管外观，prompt 管动作和表情）
- 场景描述越具体越好
- 动作描述用进行时态

## 视觉反模式（KlingAI 做不好的）
- 镜面反射/水面倒影：脸部融化、衣着错乱 → 改用直接面对面拍摄
- 透明/半透明介质：玻璃门、窗户看人 → 改用角色直接出现在画面中
- 3人及以上同框：角色融合或消失 → 控制同框人数 ≤ 2
- 屏幕/书籍上的文字：会出现乱码 → 避免特写文字
- 精细手部交互：递物件、系扣子 → 用中景降低手部细节要求

## 输出格式

先写整体剧本，再写 SegmentPlan。**在同一个 JSON 里**：

```json
{
  "story": {
    "logline": "一句话概括这个视频讲什么故事",
    "emotion_arc": "情绪弧线：从什么状态 → 经过什么 → 到什么状态",
    "segment_outline": [
      "段1：叙事功能（铺垫/展开/高潮/收束）+ 这段要表达什么",
      "段2：承接上段的什么 → 推进到什么",
      "段3：..."
    ]
  },
  "segments": [
    {
      "segment_index": 0,
      "scene_description": "现代都市，傍晚暖光。宋玉家客厅，柔和夕阳从落地窗照进来",
      "perspective": "first_person",
      "duration_seconds": 12,
      "aspect_ratio": "16:9",
      "transition_to_next": "hard_cut",
      "characters": [
        {"character_id": "songyu", "outfit_item_id": "从衣橱匹配的 item_id"}
      ],
      "shots": [
        {
          "shot_index": 0,
          "scale": "中景",
          "camera_movement": "固定",
          "duration_seconds": 3,
          "shot_prompt": "宋玉坐在餐桌前，手撑着头，慵懒地嚼着面包，眼睛半眯"
        },
        {
          "shot_index": 1,
          "scale": "近景",
          "camera_movement": "推",
          "duration_seconds": 5,
          "shot_prompt": "宋玉拿起手机看了一眼，嘴角微微上扬，低声说："嗯……知道了""
        }
      ],
      "prompt": "（备用，如果 shots 为空才用）"
    }
  ]
}
```

- shots 必填，每段至少 1 个 shot，shot 时长之和等于段时长
- shot_prompt 用中文，写动作+表情+对话，不写衣服
