# 围棋复盘工作流（野狐棋谱 + KataGo + LLM）

这个项目用于批量复盘学生最近一段时间的野狐围棋棋谱（`*.sgf`）：

1. 读取最近棋谱。
2. 用 KataGo 逐步评估每一步的质量。
3. 自动提取关键问题手（胜率下降）。
4. 汇总短板并输出中文教学建议（可选接入 LLM）。

## 1. 准备材料

- 学生最近对局 SGF 文件（建议放到同一目录）。
- KataGo 可执行文件、配置文件、模型文件。
- 可选：OpenAI API Key（用于生成更自然的教学建议）。

## 2. 运行方式

```bash
python3 go_review.py \
  --sgf-dir /path/to/sgf \
  --out-dir /path/to/output \
  --katago-bin /path/to/katago \
  --katago-config /path/to/analysis_example.cfg \
  --katago-model /path/to/model.bin.gz \
  --player-name 学生野狐ID \
  --student-color auto \
  --recent-days 30 \
  --max-games 20 \
  --max-visits 600
```

如果不想使用 LLM，默认即可（`--llm-provider none`）。

## 3. 使用 LLM（可选）

```bash
export OPENAI_API_KEY="你的key"

python3 go_review.py \
  --sgf-dir /path/to/sgf \
  --out-dir /path/to/output \
  --katago-bin /path/to/katago \
  --katago-config /path/to/analysis_example.cfg \
  --katago-model /path/to/model.bin.gz \
  --player-name 学生野狐ID \
  --llm-provider openai \
  --llm-model gpt-4.1-mini
```

自定义 API 地址（例如兼容 OpenAI 协议的本地大模型网关）：

```bash
--llm-base-url https://your-api-host/v1
```

## 4. 输出内容

在 `--out-dir` 下会生成：

- `student_review_report.md`：总报告（可直接发给学生/家长）。
- `summary.json`：汇总统计（便于二次开发）。
- `games/*.json`：每局详细问题手数据。

## 5. 常用参数

- `--min-winrate-drop 0.04`：问题手阈值（默认4%）。
- `--student-color auto|B|W|both`：学生执子颜色识别。
- `--recent-days 30`：按文件修改时间筛最近天数。
- `--max-games 30`：最多分析局数。
- `--max-visits 500`：KataGo计算强度（越大越慢）。

## 6. 注意事项

- 本脚本按 SGF 主线复盘，不处理分支变化图。
- 当 `--student-color auto` 且匹配不到 `--player-name` 时，会分析双方落子。
- 若你直接从野狐导出 SGF，建议先保证文件名中包含日期，便于管理最近对局样本。
