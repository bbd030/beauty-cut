# 妆影切片 · BeautyCut

把美妆成品短视频拆分为带起止时间和预览图的镜头清单，为后续打标、检索和重组准备输入。

当前已实现批量素材完整性校验、视频属性探测和完整解码验证。镜头切分尚未实现。开发规则见 [AGENTS.md](AGENTS.md)，原始基线见 [docs/BASELINE.md](docs/BASELINE.md)，评测记录见 [docs/EVALUATIONS.md](docs/EVALUATIONS.md)。

原始题目和三条测试视频仅在本地 `aios-shot-materials/` 中保存，不随公开仓库分发。首次使用者需自行取得获准使用的素材。

## 已确认的镜头边界与运行环境（2026-11-13）

- 按视觉剪辑点划分镜头：切换到另一段拍摄、另一机位、另一场景或独立的产品特写时，考虑设为边界；同机位出现明显剪辑跳切也属于候选边界。
- 同一段连续拍摄中的转头、抬手、移动产品或化妆动作变化，通常不切；不能仅凭字幕、亮度或化妆步骤变化判定换镜头。
- 例如“人物脸部 → 独立口红特写 → 人物涂口红”通常为三个镜头；连续拍摄中先画眉再涂口红通常仍为一个镜头。化妆步骤识别留待后续打标，具体阈值和渐变转场落点在实现与验证时确定。
- 面向大部分普通电脑：优先使用普通 CPU、本地处理，不要求独立显卡；目标兼容 Windows、macOS 和 Linux。跨平台兼容是实施目标，已验证的具体系统须单独记录，不能把目标写成已通过的测试结论。

## 实现顺序

1. 已实现：批量素材完整性校验，记录逐项失败并继续。
2. 已实现：视频属性探测和解码环境验证。
3. 镜头边界检测及起止时间输出。
4. 镜头预览图和实际切分检查。

每一步单独验证、提交和推送；其余条目为后续计划。

## 运行素材校验

需要 Python 3.11 或更新版本；素材校验仅使用标准库，不需要视频工具。从项目根目录运行：

```sh
python -m beauty_cut verify --manifest aios-shot-materials/SHA256SUMS.json
```

清单格式为 JSON 对象：键为相对于清单目录的文件路径，值为 64 位十六进制 SHA-256。支持 UTF-8（含 BOM）、中文与子目录；不读取清单目录之外的文件。原始清单包含题目说明和三条视频，全部检查，不局限于 MP4。

命令向标准输出写入 JSON，含 `schema_version`、`summary` 和逐文件 `results`。每项包含 `file`、`status`、`expected_sha256`、`actual_sha256` 和 `error`。成功时 `error` 为 null；未计算哈希时 `actual_sha256` 为 null。清单整体无效时返回含 `schema_version` 和 `error` 的 JSON。

退出码：`0` 全部一致；`1` 至少一个条目失败（仍检查其余条目）；`2` 清单不可读、格式无效或命令行用法错误。用法错误由 argparse 输出到标准错误。若需保存报告，可将标准输出重定向到本地文件。

此命令只校验文件字节完整性，不验证视频能否解码，也不检测镜头。

## 运行视频探测与完整解码检查

Python 代码仍只使用标准库。此命令另需手动准备 FFmpeg 和 FFprobe：从 [FFmpeg 官方下载页](https://ffmpeg.org/download.html)选择适合系统的构建或软件包，确认两者均可运行。建议使用 FFmpeg 6 或更新版本；本项目实际验证的工具版本见验证记录。Windows 可解压便携版，macOS/Linux 可用系统包管理器安装。程序不自动下载，不上传素材，不要求显卡。

先检查工具，再在项目根目录指定一个或多个本地视频（有空格时加引号）：

```sh
ffmpeg -version
ffprobe -version
python -m beauty_cut probe "video-1.mp4" "video-2.mp4"
```

默认通过 PATH 找工具。便携工具或未加入 PATH 时，使用可执行文件的路径；两个参数需分别指定，例如 Windows：

```powershell
python -m beauty_cut probe "video-1.mp4" "video-2.mp4" --ffmpeg ".local/tools/ffmpeg/bin/ffmpeg.exe" --ffprobe ".local/tools/ffmpeg/bin/ffprobe.exe" --timeout 600
```

上述工具路径是占位示例，替换为实际解压目录。macOS/Linux 同样支持 `--ffmpeg`、`--ffprobe`，使用对应系统的可执行文件路径。工具须由用户安装，项目不分发工具二进制。

报告只向标准输出写 JSON，沿用 `schema_version: 1`、`summary`（`total/passed/failed`）、`results`；新增 `tools` 记录工具版本。每条结果包含：

| 字段 | 含义 |
| --- | --- |
| `file`、`status`、`error` | 输入路径、`passed/failed`、失败原因；成功时 `error` 为 null，失败信息标明 `input/probe/decode` 阶段 |
| `metadata` | 容器、视频编码、编码宽高、时长（秒）、平均/标称帧率、时间基准、旋转角度；属性探测失败时为 null |
| `decode` | `status` 为 `not_run/passed/failed`；`frames` 为成功完成后的实际输出帧数，未成功时为 null |

`metadata` 的具体键为 `container/codec/width/height/duration_seconds/average_fps/nominal_fps/time_base/rotation_degrees`。可选属性缺失、帧率为 `0/0` 等情况保留 null。时长优先用视频轨时长，缺失时用容器时长；宽高是编码尺寸，旋转角度单独报告。平均和标称帧率均不构成固定帧率保证；后续定位镜头须使用实际时间戳，不能仅按帧数除以标称帧率。

只检查第一条非封面视频轨，忽略音频、字幕及其他视频轨。视频按顺序处理，CPU 解码且限制解码线程数为 2；从头读取到结束，出现解码错误即记失败，不写出转码视频或预览图。无视频轨、零帧、文件缺失、工具错误和超时均有明确记录；探测成功后解码失败仍保留属性。输入限本地普通文件，工具的输入协议限制为本地文件，不能传 URL 或网络播放列表。

`--timeout` 默认 600 秒，限制每条视频的属性探测和解码各自的最大时间；工具启动检查每个最多 10 秒。超时会终止对应进程并继续下一条视频；慢电脑或长视频可适当调大，须为有限正数。

退出码：`0` 所有视频通过；`1` 至少一条失败且已继续其余条目；`2` 工具缺失/无法启动、无效超时或命令行错误。环境配置错误输出含 `schema_version/error` 的 JSON；命令行用法错误仍由 argparse 输出到标准错误。`verify` 与 `probe` 分别运行，不自动串联，也不会把哈希校验通过计入解码通过。

保存本地报告可先创建被忽略的 `outputs/`，再将标准输出重定向到 `outputs/probe.json`。不要把报告或素材画面加入公开仓库。

解码通过表示本次工具成功读取到文件结束，不代表音频检查、镜头检测或官方评测通过；没有参考原片时，不能识别所有被重新封装成有效文件的截短内容。命令选项依据 [FFmpeg 文档](https://ffmpeg.org/ffmpeg.html)和 [FFprobe 文档](https://ffmpeg.org/ffprobe.html)。

## 测试

```sh
python -m unittest discover -s tests -v
```

测试在临时目录中生成合成数据，不需要题目素材、网络或云服务。单元测试不依赖 FFmpeg；视频集成测试需同时找到 FFmpeg/FFprobe，否则明确跳过。工具可以位于 PATH，也可通过环境变量 `BEAUTYCUT_FFMPEG`、`BEAUTYCUT_FFPROBE` 指定（仅供测试，业务命令使用上述参数）。不要把跳过集成测试说成解码验证通过。最新验证记录见 [docs/VALIDATION.md](docs/VALIDATION.md)。
