## 2026-06-15 04:45:01 +08:00

作者模型：Codex / GPT-5

目标：整理项目收尾状态，保留可复用交付物和主线开发记录。

关键动作：
- 最终交付物确定为纯覆盖 ZIP：`release/TheStanleyParable_CN_Overlay_20260615_zhcn_v3.zip`。
- 项目 README 与包内 README 保留安装、恢复、字体和覆盖行为说明。
- 用户实测主菜单、选项页和游戏内汉化生效。
- 后续 loading 后闪退经事件日志和公开案例对照，判断更符合原版游戏在高核心 CPU 上的兼容性问题；推荐用 Steam 启动项 `-threads 1` 做诊断，再测试 `-threads 4`、`-threads 8` 选择稳定值。

变更文件：
- `DEVELOPMENT_LOG.md`

验证结果：
- `release` 目录只剩 `TheStanleyParable_CN_Overlay_20260615_zhcn_v3.zip`。
- 最终 ZIP SHA256：`003D8564DBA02950595160BDCEA4E6BE400C0BEF9E2E51AF84E49852B6E7AFC4`。
- `python .\tools\localize.py audit` 返回 1343 行、0 fatal、0 warn。
- Windows 事件日志中闪退模块为 `datacache.dll`，异常码 `0xc0000005`；用户 CPU 为 24 核 / 32 逻辑线程，和公开的高核心 loading 崩溃案例相符。

## 2026-06-15 04:03:30 +08:00

作者模型：Codex / GPT-5

目标：修复运行时 UI 漏翻和字体显示问题，生成最终 v3 覆盖包。

关键动作：
- 使用 Google Fonts 官方 Noto Sans SC TTF 作为中文字体，并通过 Source scheme 的 `CustomFontFiles` 随包加载。
- 保留主标题 `THE STANLEY PARABLE` 的原版 `League Gothic` 字体，只将中文 UI、弹窗和字幕相关字体切换到 Noto Sans SC。
- 降低主菜单中文菜单项字号，避免中文 fallback 字体过大和标题截断。
- 处理 `mainmenu_tsp.res`、`options.res`、`extrasdialog.res` 中的可见硬编码文本：
  - `Credits` -> `制作名单`
  - `Extras` -> `额外内容`
  - `Achievement` -> `成就`
  - `Saves` -> `存档`
- 重新构建并安装到 `G:\SteamLibrary\steamapps\common\The Stanley Parable` 做文件级验证。

变更文件：
- `README.md`
- `tools/localize.py`
- 生成物：`dist/`、`release/TheStanleyParable_CN_Overlay_20260615_zhcn_v3.zip`

验证结果：
- `python -m py_compile .\tools\localize.py` 通过。
- `python .\tools\localize.py audit` 返回 1343 行、0 fatal、0 warn。
- `python .\tools\localize.py build` 与 `package --version 20260615_zhcn_v3` 成功，v3 包包含 24 个覆盖资源。
- 可见 `.res` 英文硬编码扫描只剩保留的原版标题。
- 安装后对 `dist\thestanleyparable` 与游戏目录内 24 个覆盖文件做 SHA256 比对，全部一致。

后续事项：
- 无。

## 2026-06-15 03:31:38 +08:00

作者模型：Codex / GPT-5

目标：完成《The Stanley Parable》简体中文本地化主流程，使用 Gemini 完成翻译和审校，并生成可复用汉化包。

关键动作：
- 从 `G:\SteamLibrary\steamapps\common\The Stanley Parable` 提取 1343 条文本，其中旁白 674 条、音效字幕 266 条、UI 403 条。
- 按剧情路线和 UI 类型划分为 11 个上下文组、24 个批次，使模型在同一剧情线内连续理解上下文。
- 准备游戏背景、术语表和翻译要求：史丹利、旁白、精神控制设施、会议室、老板办公室、扫帚间、冒险线、严肃房间等术语统一。
- 通过 Gemini CLI 完成初译；审校阶段强制使用 `gemini-3.1-pro-preview`，全量审校 24 个批次，采纳 66 条修正。
- 最终由当前模型人工抽查高修正批次、剧情分组样本、UI 长度、专名、变量和标记，修正 UI 省略号、按键变量引号和个别文本问题。
- 生成英文覆盖资源与 `schinese` 副本，确保原版游戏默认启动即可显示中文。

变更文件：
- `.gemini/settings.json`
- `.gitignore`
- `README.md`
- `tools/localize.py`
- 生成物：`data/`、`dist/`、`release/`、`assets/fonts/`、`reports/`、`logs/`

验证结果：
- Gemini 审校输出累计修正 66 条。
- `extract` 对英文 `subtitles_english.dat` 与 `closecaption_english.dat` 做二进制往返校验通过，支持 DAT 解析与编译逻辑正确。
- `audit` 最终结果为 1343 行、0 fatal、0 warn，支持无缺漏和无硬标签/变量丢失。
- 构建后 `subtitles_english.dat`、`subtitles_schinese.dat` 均可解析为 674 条；`closecaption_english.dat`、`closecaption_schinese.dat` 均可解析为 266 条。
- 占位符、颜色标签、字幕控制标签多重集合检查为 0 个问题。
- UI 长度风险扫描未发现中文显示宽度明显超过英文的高风险按钮/菜单项。

后续事项：
- 无。
