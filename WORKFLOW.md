# 围棋教学自动生成流程

## 概述
每收到学生的新棋谱 → 自动分析 → 生成个性化教学网站

## 流程步骤

### 1. 准备阶段（已完成）
- [x] KataGo 分析工具
- [x] SGF 棋谱解析
- [x] 教学数据生成脚本
- [x] 网页模板

### 2. 新棋谱分析流程

```bash
# 步骤1: 用 KataGo 分析新棋谱
python3 go_review.py \
  --sgf-dir ./new_games \
  --student-name 芒果25437 \
  --output ./new_analysis

# 步骤2: 生成教学数据
python3 build_kid_teaching_data.py

# 步骤3: 复制到网站目录
cp review_output_mgqp_full28/kid_teaching_data.js docs/
cp review_output_mgqp_full28/kid_teaching_data.json docs/
```

### 3. 自动化脚本

创建 `analyze_new_game.py`:

```python
#!/usr/bin/env python3
"""分析学生的新棋谱，生成教学网站"""

import sys
import json
from pathlib import Path
from go_review import analyze_game
from build_kid_teaching_data import build_teaching_data

def main(sgf_path, student_id):
    # 1. 分析棋谱
    print(f"📊 分析棋谱: {sgf_path}")
    analysis = analyze_game(sgf_path, student_id)
    
    # 2. 与历史错误点对比
    print("🔍 对比历史薄弱环节...")
    weak_points = compare_with_history(analysis)
    
    # 3. 生成教学数据
    print("📝 生成教学数据...")
    teaching_data = build_teaching_data(analysis, weak_points)
    
    # 4. 输出到网站目录
    output_path = Path("docs/kid_teaching_data")
    output_path.mkdir(exist_ok=True)
    # ... 保存文件
    
    print(f"✅ 完成！访问 docs/index.html 查看")

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "芒果25437")
```

## 使用方法

### 方式1: 手动
1. 把新棋谱放入 `data/new_games/` 目录
2. 运行分析脚本
3. 推送 GitHub Pages

### 方式2: 发送给我（OpenClaw）
直接发送 SGF 文件给我，我调用 Codex 执行分析

## 输出
- 个性化教学网页（类似当前 芒果25437 版本）
- 学生薄弱环节分析报告
- 针对每盘棋的错题讲解

## 技术依赖
- KataGo + 模型
- Python 3.10+
- go_review.py
- build_kid_teaching_data.py
