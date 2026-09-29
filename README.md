# 离线 HLS 窗口复原 API

按时间顺序抓取的多份 HLS 媒体清单（snapshots）只是同一个直播切片窗口的不同视图。
本服务把 **2～30 份**清单（每份 **1～100 段**）按媒体序号合并去重，复原出一条
连续、带精确小数起止秒数的切片时间线。**完全离线**：服务不发起任何网络请求，
`playlist_url` 仅用于解析相对 URI。

提供四个接口：

- `POST /v1/reconcile`：复原整条切片时间线（序号、URI、精确十进制相对起止秒数、
  字节范围、不连续计数）。
- `POST /v1/locate`：在同一份合并规则之上，解析
  `#EXT-X-PROGRAM-DATE-TIME`，按**节目绝对时间（UTC）**定位目标时刻所在的片段，
  返回片段序号、URI、UTC 起止时间与精确十进制片内偏移。
- `POST /v1/vod`：把直播窗口按十进制秒区间 `[from_sec, to_sec)` 导出为一份
  **VOD 媒体清单文本**，并返回实际覆盖起止与两端多带的秒数。
- `POST /v1/fetch-plan`：为复原后的窗口离线生成**媒体片段获取计划**——去重合并
  后的 HTTP 资源请求列表（完整资源请求 / `Range` 字节范围请求），以及每段在响应体
  中的字节映射。

## 单条命令启动

```bash
docker compose up --build
```

服务监听 **8000** 端口（容器内 `8000`，映射到宿主机 `8000`）：

- API：`POST http://localhost:8000/v1/reconcile`、`POST http://localhost:8000/v1/locate`、`POST http://localhost:8000/v1/vod`、`POST http://localhost:8000/v1/fetch-plan`
- 健康检查：`GET http://localhost:8000/healthz`
- 交互式文档：`http://localhost:8000/docs`

> 改用其他宿主端口：`HOST_PORT=9000 docker compose up --build`（需在
> `docker-compose.yml` 中把端口写成 `"${HOST_PORT:-8000}:8000"`，或直接编辑该文件）。

不用 Docker 时：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 请求

`POST /v1/reconcile`

```json
{
  "playlist_url": "https://cdn.example.com/live/index.m3u8",
  "snapshots": ["#EXTM3U\n...第 0 份清单文本...", "#EXTM3U\n...第 1 份..."]
}
```

| 字段 | 说明 |
| --- | --- |
| `playlist_url` | 清单的 URL，仅作为相对 URI 的解析基址，不会被请求 |
| `snapshots` | **按抓取时间顺序**排列的清单原文数组；长度 2～30 |

## 响应（200）

```json
{
  "playlist_url": "https://cdn.example.com/live/index.m3u8",
  "count": 3,
  "duration": "6.833",
  "segments": [
    {
      "sequence": 100,
      "uri": "https://cdn.example.com/live/big.ts",
      "duration": "2.000",
      "discontinuity": 3,
      "byte_offset": 0,
      "byte_length": 100,
      "start": "0",
      "end": "2.000"
    },
    {
      "sequence": 101,
      "uri": "https://cdn.example.com/live/big.ts",
      "duration": "1.500",
      "discontinuity": 4,
      "byte_offset": 100,
      "byte_length": 50,
      "start": "2.000",
      "end": "3.500"
    }
  ]
}
```

- `duration` / `start` / `end` 为**精确十进制**字符串（内部用 `Decimal` 计算，
  不经过二进制浮点），`start`/`end` 为秒级时间轴上的半开区间 `[start, end)`。
- 没有字节范围的段，`byte_offset` 与 `byte_length` 为 `null`。
- `discontinuity` 为复原后的不连续计数（见下）。

## 解析与合并规则

支持的标签：`#EXTM3U`、`#EXT-X-MEDIA-SEQUENCE`、`#EXTINF`、
`#EXT-X-DISCONTINUITY-SEQUENCE`、`#EXT-X-DISCONTINUITY`、
`#EXT-X-BYTERANGE`、`#EXT-X-PROGRAM-DATE-TIME`；其余标签一律忽略。
（`#EXT-X-PROGRAM-DATE-TIME` 仅 `/v1/locate` 使用，对 `/v1/reconcile` 无影响。）

1. **序号**：段序号从 `EXT-X-MEDIA-SEQUENCE`（缺省为 `0`）起逐段加一。
2. **URI**：段 URI 按 RFC 3986 相对 `playlist_url` 解析（`urljoin`）。
3. **时长**：`EXTINF` 时长按十进制数解析并原样保留精度（如 `1.10`）。
4. **不连续计数**：以 `EXT-X-DISCONTINUITY-SEQUENCE`（缺省为 `0`）为基数，
   每遇到一个 `#EXT-X-DISCONTINUITY` 标记计数加一，随后一段携带新计数值。
5. **字节范围**：`EXT-X-BYTERANGE:n@o` 取显式偏移 `o`；仅写作 `n` 而无偏移时，
   只允许承接**同一份清单内紧邻的前一段**，且该段必须满足：
   - URI 与当前段完全相同，并且
   - 具有已确定的字节范围；

   此时偏移 = 前段偏移 + 前段长度。否则该清单视为非法。
6. **跨快照合并**：以序号为主键合并。后一份快照：
   - 起始序号 **小于前一份快照的起始序号**（序号回退）→ 422；
   - 与已合并内容不重叠时，起始序号必须**恰好等于已合并末序号 + 1**，按序号
     首尾相接即可合并；若 **大于已合并末序号 + 1**（出现空洞）→ 422；
   - 与旧段重叠时，**URI、十进制时长、解析后的字节范围、不连续计数** 四者必须
     完全一致，任一不同 → 422。
7. 合并后从最早序号到最晚序号必须连续无缺，再从 0 秒起逐段累加时长，输出每段的
   精确 `start`/`end`。

## 错误响应（422）

所有复原类错误均为 HTTP `422`，并在响应体中给出**快照下标（从 0 起）与序号**：

```json
{
  "error": "overlap_conflict",
  "detail": "snapshot 1 conflicts with an earlier snapshot at sequence 101",
  "snapshot_index": 1,
  "sequence": 101
}
```

| `error` | 触发条件 |
| --- | --- |
| `invalid_request` | 快照数量不在 2～30、字段缺失/类型错误等（无具体快照时无定位字段） |
| `invalid_playlist` | 清单无法解析（缺 `#EXTM3U`、`EXTINF` 非法、字节范围承接失败等），带 `snapshot_index` |
| `empty_playlist` | 快照内没有任何段，带 `snapshot_index` |
| `too_many_segments` | 单份快照超过 100 段，带 `snapshot_index` |
| `sequence_rollback` | 起始序号回退，带 `snapshot_index` 与 `sequence` |
| `sequence_gap` | 后份快照起点超过已合并末序号 + 1，带 `snapshot_index` 与 `sequence` |
| `overlap_conflict` | 重叠序号的 URI/时长/字节范围/不连续计数不一致，带 `snapshot_index` 与 `sequence` |

## `POST /v1/locate`：按节目绝对时间定位片段

请求体复用 `/v1/reconcile` 的 `playlist_url` 与 `snapshots`（合并、回退、空洞、
重叠冲突等规则完全相同），额外接收一个**必须带时区偏移**的 RFC 3339 时刻：

```json
{
  "playlist_url": "https://cdn.example.com/live/index.m3u8",
  "snapshots": ["#EXTM3U\n...", "#EXTM3U\n..."],
  "target_at": "2024-01-02T03:04:07.5+08:00"
}
```

| 字段 | 说明 |
| --- | --- |
| `playlist_url` | 同 `/v1/reconcile`，仅作相对 URI 解析基址，不会被请求 |
| `snapshots` | 同 `/v1/reconcile`，按抓取时间排序，长度 2～30 |
| `target_at` | 目标时刻，RFC 3339 日期时间，**必须**带时区（`Z` 或 `±HH:MM`）；无时区 → 422 |

### 绝对时间推算规则

1. **锚点归属**：每个 `#EXT-X-PROGRAM-DATE-TIME` 标签锚定**紧随其后的那个片段的
   起点**。标签必须出现在某片段之前（悬空标签 → `invalid_playlist`）。锚点按其序号
   归属到合并时间线上**实际包含该序号的连续序号区间**（相邻片段断点计数不变即为
   同一区间），而**不是按断点计数值查表**。
2. **归一为 UTC**：标签和 `target_at` 的时区偏移先归一到 UTC。内部一律用
   `Decimal` 精确十进制 POSIX 秒表示，小数秒按原样保留精度，全程不经过二进制浮点。
3. **仅在同一连续序号区间内推算**：从锚点出发，按各片段 `EXTINF` 的精确十进制
   时长，向序号增大/减小两个方向逐段累加/累减，得到区间内每段的 UTC 起止时间。
   **绝不跨越 `#EXT-X-DISCONTINUITY` 断点推算**：无锚点的区间没有任何已知时间覆盖。
   断点计数值不是区间的唯一标识——两份无重叠、按序号相接的快照可能在**另一段连续
   序号区间复用同一计数值**；这样的两个区间互相独立，各自独立校验锚点、独立推算，
   时间既不跨断点、也不跨快照复用区间传播。
4. **跨快照同序号锚点一致**：不同快照中给同一序号片段的
   `#EXT-X-PROGRAM-DATE-TIME`，归一 UTC 后必须是同一时刻，否则 422
   （`program_time_conflict`，带后出现锚点的 `snapshot_index` 与 `sequence`）。
5. **同一区间多锚点一致**：同一连续序号区间内出现多个锚点时，后一锚点必须恰好等于
   「首锚点 + 两序号间精确时长之差」，与推算结果逐位一致，否则 422
   （同样带 `snapshot_index` 与 `sequence`）。计数复用的两个区间各自独立执行本校验，
   不会因计数值相同而被当作同一区间。

### 查找与响应

按片段的**半开区间 `[start_at, end_at)`** 查找 `target_at`：恰好落在片段起点上算
命中，落在片段终点上则归入后一段。命中返回 `200`：

```json
{
  "sequence": 101,
  "uri": "https://cdn.example.com/live/seg1.ts",
  "start_at": "2024-01-01T19:04:06.1Z",
  "end_at": "2024-01-01T19:04:08.3Z",
  "offset": "1.40"
}
```

- `start_at` / `end_at`：片段在 **UTC** 下的绝对起止（RFC 3339，`Z` 结尾，
  含必要的精确小数秒）。
- `offset`：目标时刻相对片段起点的**精确十进制片内秒偏移**（`[0, duration)`）。

未命中与冲突：

| 状态码 | `error` | 触发条件 |
| --- | --- | --- |
| 404 | `program_time_uncovered` | 没有任何锚点，或目标时刻不在任何已锚定连续区间的已知覆盖范围内（含落在断点另一侧、或落在计数复用但自身无锚点的区间内） |
| 422 | `ambiguous_program_time` | 目标时刻在**多个已锚定连续区间**各自的覆盖段内同时命中（断点两侧节目时间重叠；计数复用的两个独立区间同样可能重叠），响应带 `sequences` 候选序号数组 |
| 422 | `program_time_conflict` | 跨快照同序号锚点 UTC 不一致，或同区间多锚点与十进制时长推算不一致；带 `snapshot_index`、`sequence` |
| 422 | `invalid_playlist` | `#EXT-X-PROGRAM-DATE-TIME` 非法（不是带时区的 RFC 3339、悬空等），带 `snapshot_index`（可知时还带 `sequence`） |
| 422 | `invalid_request` | `target_at` 缺失/无时区/格式错误，或快照数量、字段类型不满足要求 |

此外，`/v1/reconcile` 的全部合并类错误（`sequence_rollback`、`sequence_gap`、
`overlap_conflict` 等）在 `/v1/locate` 同样可能返回，状态码均为 422。

```bash
curl -s http://localhost:8000/v1/locate \
  -H 'Content-Type: application/json' \
  -d '{
    "playlist_url": "https://cdn.example.com/live/index.m3u8",
    "snapshots": [
      "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:0\n#EXT-X-PROGRAM-DATE-TIME:2024-01-02T03:04:05+08:00\n#EXTINF:1.10,\nseg0.ts\n#EXTINF:2.20,\nseg1.ts\n",
      "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:1\n#EXTINF:2.20,\nseg1.ts\n#EXTINF:0.25,\nseg2.ts\n"
    ],
    "target_at": "2024-01-01T19:04:07.5Z"
  }'
```

## `POST /v1/vod`：导出指定时段的 VOD 媒体清单

请求体在 `playlist_url` 与 `snapshots` 之外，接收十进制秒区间
`[from_sec, to_sec)`（半开）：

```json
{
  "playlist_url": "https://cdn.example.com/live/index.m3u8",
  "snapshots": ["#EXTM3U\n...", "#EXTM3U\n..."],
  "from_sec": "1.5",
  "to_sec": "6.0"
}
```

| 字段 | 说明 |
| --- | --- |
| `playlist_url` | **必须是绝对 `http(s)` URL**；仅作相对 URI 的解析基址，不会被请求 |
| `snapshots` | 同 `/v1/reconcile`，按抓取时间排序，长度 2～30，每份 1～100 段 |
| `from_sec` / `to_sec` | 十进制秒（字符串或数值均可），必须满足 `0 ≤ from_sec < to_sec ≤ 合并后总时长` |

### 选段与清单规则

1. **合并**：先执行与 `/v1/reconcile` 完全相同的合并（回退、空洞、重叠冲突等
   原样返回 422）。
2. **禁用标签**：任一份快照含 `#EXT-X-KEY` 或 `#EXT-X-MAP` → 422
   （`unsupported_tag`，带 `snapshot_index`）。
3. **选段**：选取与 `[from_sec, to_sec)` **相交的首、末片段以及其间全部片段**
   （段 `[start, end)` 满足 `start < to_sec` 且 `end > from_sec` 即入选）。首末段
   可能超出请求区间，实际多带的秒数在响应中返回。
4. **固定标签**：输出清单依次写入 `#EXTM3U`、`#EXT-X-VERSION:4`、
   `#EXT-X-MEDIA-SEQUENCE:0`、`#EXT-X-PLAYLIST-TYPE:VOD`、
   `#EXT-X-TARGETDURATION`、段列表、`#EXT-X-ENDLIST`。
   `#EXT-X-TARGETDURATION` 为**所选片段最长时长的向上取整**。
5. **逐段输出**：`#EXTINF` 保留源清单中的**原精度**十进制时长；段 URI 为相对
   `playlist_url` 解析后的**绝对 URI**；有字节范围的段总写
   `#EXT-X-BYTERANGE:length@offset`（无显式偏移的承接段也会展开为绝对偏移）。
6. **不连续**：相邻输出片段的不连续计数发生变化时，在后一段前插入
   `#EXT-X-DISCONTINUITY`；清单第一段之前不插入。

### 响应（200）

```json
{
  "playlist_url": "https://cdn.example.com/live/index.m3u8",
  "from_sec": "1.5",
  "to_sec": "6.0",
  "covered_start": "0",
  "covered_end": "6.833",
  "head_extra_sec": "1.5",
  "tail_extra_sec": "0.833",
  "segment_count": 3,
  "playlist": "#EXTM3U\n#EXT-X-VERSION:4\n#EXT-X-MEDIA-SEQUENCE:0\n#EXT-X-PLAYLIST-TYPE:VOD\n#EXT-X-TARGETDURATION:4\n#EXTINF:2.000,\nhttps://cdn.example.com/live/seg100.ts\n#EXTINF:1.500,\nhttps://cdn.example.com/live/seg101.ts\n#EXTINF:3.333,\nhttps://cdn.example.com/live/seg102.ts\n#EXT-X-ENDLIST\n"
}
```

- `covered_start` / `covered_end`：所选片段**实际覆盖**的起止秒数（可能超出请求
  区间）。
- `head_extra_sec` / `tail_extra_sec`：**两端多带**的精确十进制秒数，分别为
  `from_sec - covered_start` 与 `covered_end - to_sec`（区间恰好落在段边界上时为 0）。
- `segment_count`：输出清单中的片段数。
- `playlist`：VOD 媒体清单文本（UTF-8，`\n` 换行，末尾带换行）。

错误均为 HTTP 422：

| `error` | 触发条件 |
| --- | --- |
| `invalid_range` | 区间端点不是有限非负十进制数、`from_sec ≥ to_sec`、或 `to_sec` 超过合并后总时长 |
| `unsupported_tag` | 快照含 `#EXT-X-KEY` / `#EXT-X-MAP`，带 `snapshot_index` |
| `invalid_request` | `playlist_url` 不是绝对 http(s) URL、快照数量不合法、字段缺失/类型错误等 |
| 其余合并类错误 | `sequence_rollback`、`sequence_gap`、`overlap_conflict`、`invalid_playlist` 等，同 `/v1/reconcile` |

```bash
curl -s http://localhost:8000/v1/vod \
  -H 'Content-Type: application/json' \
  -d '{
    "playlist_url": "https://cdn.example.com/live/index.m3u8",
    "snapshots": [
      "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:100\n#EXTINF:2.000,\nseg100.ts\n#EXTINF:1.500,\nseg101.ts\n#EXTINF:3.333,\nseg102.ts\n",
      "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:102\n#EXTINF:3.333,\nseg102.ts\n#EXTINF:2.000,\nseg103.ts\n"
    ],
    "from_sec": "1.5",
    "to_sec": "6.0"
  }'
```

## `POST /v1/fetch-plan`：生成媒体片段获取计划

请求体与 `/v1/reconcile` 相同（`playlist_url` + 按时间排序的 `snapshots`），
沿用完全相同的合并与错误规则，**离线**返回一份「资源请求 + 逐段映射」计划：

```json
{
  "playlist_url": "https://cdn.example.com/live/index.m3u8",
  "snapshots": ["#EXTM3U\n...", "#EXTM3U\n..."]
}
```

| 字段 | 说明 |
| --- | --- |
| `playlist_url` | **必须是绝对 `http(s)` URL**；仅作相对 URI 的解析基址，不会被请求 |
| `snapshots` | 同 `/v1/reconcile`，按抓取时间排序，长度 2～30，每份 1～100 段 |

### 请求合并规则

1. **先合并时间线**：执行与 `/v1/reconcile` 完全相同的合并（回退、空洞、重叠
   冲突等原样返回 422）。
2. **拒绝非 HTTP(S) 片段 URI**：合并后任一片段的绝对 URI 不是
   `http`/`https` → 422（`unsupported_uri`，带 `sequence`）。
3. **拒绝密钥与初始化段**：任一份快照含 `#EXT-X-KEY` 或 `#EXT-X-MAP` → 422
   （`unsupported_tag`，带 `snapshot_index`）。
4. **完整资源引用**：某段没有 `EXT-X-BYTERANGE` 即表示引用其 URI 的**完整资源**。
   **相同绝对 HTTP(S) URI 的完整资源只请求一次**（不带 `Range` 的普通 GET），
   该请求同时覆盖同一 URI 上的所有字节范围片段。
5. **仅字节范围引用**：某 URI 没有任何完整资源引用时，把同 URI 各段的字节半开
   区间 `[offset, offset+length)` **按偏移合并所有重叠或首尾相邻的区间**
   （`next_start ≤ prev_end` 即合并），每个合并区间生成一个
   `Range: bytes=起点-终点`（终点为闭区间）请求。
6. **请求排序与索引**：请求 `index` 从 0 起，**按首次涉及到的片段序号**从小到大
   分配（沿合并时间线扫描，第一次用到该请求时编号）。

### 逐段映射

响应的 `segments` 按片段序号排列，逐段给出四项：

| 字段 | 说明 |
| --- | --- |
| `sequence` | 片段媒体序号 |
| `request_index` | 承载该片段的请求索引（对应 `requests[].index`） |
| `body_offset` | 该片段在**响应体**内的起始字节；字节范围片段为「原偏移 − 请求起点」，完整资源片段为 `null` |
| `length` | 该片段在响应体内的字节长度；完整资源片段为 `null` |

### 响应（200）

```json
{
  "playlist_url": "https://cdn.example.com/live/index.m3u8",
  "request_count": 2,
  "segment_count": 4,
  "requests": [
    {"index": 0, "uri": "https://cdn.example.com/live/big.ts", "range": "bytes=0-149"},
    {"index": 1, "uri": "https://cdn.example.com/live/big.ts", "range": "bytes=300-499"}
  ],
  "segments": [
    {"sequence": 0, "request_index": 0, "body_offset": 0,   "length": 100},
    {"sequence": 1, "request_index": 0, "body_offset": 100, "length": 50},
    {"sequence": 2, "request_index": 1, "body_offset": 0,   "length": 100},
    {"sequence": 3, "request_index": 1, "body_offset": 100, "length": 100}
  ]
}
```

- `requests[].range`：完整资源请求为 `null`（不发送 `Range` 头）；字节范围请求为
  可直接作为 `Range` 头值的 `bytes=起点-终点`。
- 上例中序号 0/1 的范围 `[0,100)` 与 `[100,150)` 首尾相邻，合并为一次
  `bytes=0-149`；序号 2/3 的 `[300,400)` 与 `[400,500)` 合并为 `bytes=300-499`。
- 同 URI 完整资源 + 字节范围混用时，该 URI 只产生一个完整资源请求，相关各段的
  `body_offset` / `length` 均为 `null`。

错误均为 HTTP 422：

| `error` | 触发条件 |
| --- | --- |
| `unsupported_uri` | 合并后存在非 `http(s)` 的片段绝对 URI，带 `sequence` |
| `unsupported_tag` | 快照含 `#EXT-X-KEY` / `#EXT-X-MAP`，带 `snapshot_index` |
| `invalid_request` | `playlist_url` 不是绝对 http(s) URL、快照数量不合法、字段缺失/类型错误等 |
| 其余合并类错误 | `sequence_rollback`、`sequence_gap`、`overlap_conflict`、`invalid_playlist` 等，同 `/v1/reconcile` |

```bash
curl -s http://localhost:8000/v1/fetch-plan \
  -H 'Content-Type: application/json' \
  -d '{
    "playlist_url": "https://cdn.example.com/live/index.m3u8",
    "snapshots": [
      "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:1.0,\n#EXT-X-BYTERANGE:100@0\nbig.ts\n#EXTINF:1.0,\n#EXT-X-BYTERANGE:50\nbig.ts\n#EXTINF:1.0,\nclip.ts\n",
      "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:2\n#EXTINF:1.0,\nclip.ts\n#EXTINF:1.0,\nnxt.ts\n"
    ]
  }'
```

## 快速验证

```bash
curl -s http://localhost:8000/v1/reconcile \
  -H 'Content-Type: application/json' \
  -d '{
    "playlist_url": "https://cdn.example.com/live/index.m3u8",
    "snapshots": [
      "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:100\n#EXT-X-DISCONTINUITY-SEQUENCE:3\n#EXTINF:2.000,\n#EXT-X-BYTERANGE:100@0\nbig.ts\n#EXT-X-DISCONTINUITY\n#EXTINF:1.500,\n#EXT-X-BYTERANGE:50\nbig.ts\n",
      "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:101\n#EXT-X-DISCONTINUITY-SEQUENCE:3\n#EXT-X-DISCONTINUITY\n#EXTINF:1.500,\n#EXT-X-BYTERANGE:50@100\nbig.ts\n#EXTINF:3.333,\nclip.ts\n"
    ]
  }'
```

## 项目结构

```
app/
  hls.py         # 离线清单解析（序号 / 时长 / 断点 / 字节范围 / URI / PDT 锚点）
  reconcile.py   # 跨快照合并、连续性与一致性校验、时间轴计算
  locate.py      # PDT 精确 UTC 解析/推算、锚点一致性校验、半开区间定位
  vod.py         # 按秒区间选段、VOD 清单渲染（VERSION:4 / ENDLIST / 多带秒数）
  fetch.py       # 完整/字节范围请求去重合并、Range 请求生成、逐段响应体映射
  main.py        # FastAPI 路由、请求/响应模型、422/404 错误处理
tests/test_api.py
Dockerfile
docker-compose.yml
requirements.txt
```

## 测试

```bash
pip install -r requirements.txt pytest httpx
pytest -q
```
