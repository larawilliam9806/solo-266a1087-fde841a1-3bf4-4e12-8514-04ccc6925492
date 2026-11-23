# 纪录片字幕交付 API

剪辑重剪后，字幕员把完整保留的已审台词和需要复核的半句交给播出端。

- **原片字幕**：每句带编号、起止毫秒、文字；可反复换版。
- **剪辑表**：逐段填原片号、原片起止毫秒、成片起点；片段以原速剪入，同一片段可重复使用。
- **成片字幕**：整句每在成片中出现一次，就按成片时码出一条字幕。
- **待复核半句**：台词只保留一部分时，逐处列出原片号、台词编号和保留的成片时段。
- **交付**：锁定某个剪辑表版本和被引用原片的字幕版本；其中任一处换版，本次交付即被退回。

所有时段（原片和成片）都是**左闭右开**：起点计入，终点不计入。

## 快速开始

```bash
docker compose up --build
```

服务监听 `http://localhost:8000`，交互式文档在 `/docs`，SQLite 数据存放在
`subtitle-data` 卷的 `/data/subtitles.db`。

本地开发：

```bash
pip install -r requirements.txt
SUBTITLE_DB_PATH=/tmp/subtitles.db uvicorn app.main:app --reload
python -m pytest
```

## 算字幕的规则

1. 剪辑表按**成片起点**排序；任何两段在成片上不得重叠（首尾相接可以，因为终点不计入）。
2. 同一原片的两段，如果**原片上也首尾相接**并且**成片上也首尾相接**，合并为一个连续块。
   只有落进同一个连续块里的部分才算连续保留；跨块的台词一律算截断。
3. 一句台词的区间 `[start, end)` 与某个连续块的源区间相交时：
   - 若整句都被这一个块覆盖，就按 `成片起点 + (台词起点 - 块的原片起点)` 换算成一条成片字幕；
   - 否则每个相交处各产生一条待复核记录，给出保留的成片时段 `[keep_start_ms, keep_end_ms)`。
4. 同一片段在剪辑表中重复出现，就按出现次数各出一条字幕。
5. 只按本片剪辑表引用到的原片取字幕；未被引用的原片即使换了字幕版本，也不影响本次交付。

## 交付生命周期

```
创建交付 ──► open ──publish──► published（冻结成片字幕，供播出端查询）
                 └──publish──► rejected（剪辑表或任一被引用原片已换版）
```

创建交付时记录：

- 本片当前的剪辑表版本（`edit_table_version`）；
- 剪辑表引用到的每一部原片当前的**最新**字幕版本（`delivery_pins`）。

从这一刻起快照不再变化。`POST /deliveries/{id}/publish` 会重新核对这两类版本：

- 完全一致 → 生成成片字幕与待复核片段，`output_version` 递增，写入不可变发布记录；
- 有任何一处换版 → 返回 `409 delivery_stale`，本次交付记为 `rejected`，
  不会覆盖之前成功的发布。重新创建交付即可基于新版本再交付。

`GET /deliveries/{id}` 和 `/preview` 随时显示 `stale` 与 `changes`，
让字幕员在发布前就知道会不会被退回；`/preview` 不落库，可以先看结果。

## 主要接口

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| POST | `/source-reels` | 新建原片（`{"name": "A卷"}`） |
| POST | `/source-reels/{id}/subtitles` | 上交一版已审台词（自动递增版本号） |
| GET | `/source-reels/{id}/subtitles/latest` | 取某原片最新台词 |
| GET | `/source-reels/{id}/subtitles/{version}` | 取指定版本台词 |
| POST | `/films` | 新建成片 |
| POST | `/films/{id}/edit-tables` | 上交一版剪辑表（自动递增版本号） |
| GET | `/films/{id}/edit-tables/{version}` | 取指定版本剪辑表 |
| POST | `/films/{id}/deliveries` | 创建交付（快照当前剪辑表与相关台词版本） |
| GET | `/deliveries/{id}` | 交付状态、锁定版本、是否已换版 |
| GET | `/deliveries/{id}/preview` | 按快照试算成片字幕与待复核片段 |
| POST | `/deliveries/{id}/publish` | 发布；换版则退回 |
| GET | `/films/{id}/subtitles/latest` | **播出端**：最近一次成功交付的字幕 + 同次待复核片段 |
| GET | `/films/{id}/subtitles/latest?output_version=N` | 取某个历史发布版本 |
| GET | `/publications/{id}` | 按发布 id 取冻结副本 |

### 请求示例

```bash
# 原片 A 的已审台词
curl -X POST localhost:8000/source-reels/1/subtitles -H 'content-type: application/json' -d '{
  "lines": [
    {"number": 1, "start_ms": 1000, "end_ms": 4000, "text": "我们在山里住了三个月。"}
  ]
}'

# 剪辑表：B 卷开场 → A 卷整段 → A 卷接续 → B 卷开场重放
curl -X POST localhost:8000/films/1/edit-tables -H 'content-type: application/json' -d '{
  "segments": [
    {"source_reel_id": 2, "source_start_ms": 0,    "source_end_ms": 2000, "output_start_ms": 0},
    {"source_reel_id": 1, "source_start_ms": 0,    "source_end_ms": 4000, "output_start_ms": 2000},
    {"source_reel_id": 1, "source_start_ms": 4000, "source_end_ms": 6000, "output_start_ms": 6000},
    {"source_reel_id": 2, "source_start_ms": 0,    "source_end_ms": 2000, "output_start_ms": 8000}
  ]
}'

curl -X POST localhost:8000/films/1/deliveries
curl -X POST localhost:8000/deliveries/1/publish
curl localhost:8000/films/1/subtitles/latest
```

`/films/{id}/subtitles/latest` 的返回：

```json
{
  "film_id": 1,
  "output_version": 1,
  "subtitles": [
    {"source_reel_id": 2, "source_reel_name": "B卷", "line_number": 1,
     "text": "这是B卷的开场。", "start_ms": 0, "end_ms": 2000}
  ],
  "reviews": [
    {"source_reel_id": 1, "source_reel_name": "A卷", "line_number": 2,
     "text": "雪化了，路才通。", "keep_start_ms": 7000, "keep_end_ms": 8000}
  ]
}
```

`reviews` 与 `subtitles` 来自**同一次**成功交付，所以播出端可以把
「已审台词」和「需要复核的半句」一起取走。

## 数据模型

| 表 | 说明 |
| --- | --- |
| `source_reels` / `films` | 原片与成片 |
| `subtitle_documents` / `subtitle_lines` | 按原片分版本保存的已审台词 |
| `edit_tables` / `edit_segments` | 按成片分版本保存的剪辑表 |
| `deliveries` / `delivery_pins` | 一次交付及其锁定的台词版本 |
| `publications` / `publication_sources` / `publication_cues` / `publication_reviews` | 成功交付后的不可变冻结副本 |

发布时把结果写成冻结副本，之后的字幕或剪辑表换版不会改动已经交给播出端的内容。

## 错误格式

```json
{"error": {"code": "delivery_stale", "message": "...", "details": {...}}}
```

| 状态码 | 常见 `code` |
| --- | --- |
| 404 | `source_reel_not_found`、`film_not_found`、`subtitle_version_not_found`、`no_successful_delivery` |
| 409 | `delivery_stale`、`delivery_not_open`、`source_reel_exists`、`film_exists` |
| 422 | `validation_error`、`segments_overlap`、`unknown_source_reel` |
