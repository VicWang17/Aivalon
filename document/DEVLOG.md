# DEVLOG · Aivalon 开发经验与踩坑记录

> 规则（见 AGENTS.md §6）：凡开发过程中遇到的非平凡问题——报错、环境坑、压测结果与预期不符、选型纠结——都记录在此。
> 每条按「现象 → 排查 → 根因 → 解决 → 经验」五段式，注明日期。这是技术复盘长文和面试问答的一手素材库。

## 2026-08-10 · A 组观测体系

### 002 prometheus-fastapi-instrumentator 8.x 与 fastapi 的 starlette 版本冲突

- **现象**：接入指标库后 uvicorn 启动即崩，`TypeError: Router.__init__() got an unexpected keyword argument 'on_startup'`，报错栈在 `app/routers/auth.py` 的 `APIRouter()`——看上去像是自己代码的问题
- **排查**：`pip check` 一把出真相：`fastapi 0.128.0 has requirement starlette<0.51.0,>=0.40.0, but you have starlette 1.6.0`；查 instrumentator 的依赖约束发现 8.x 强制 `starlette>=1.0.0,<2.0.0`——pip 装它时把 starlette 从 0.40.x 升到了 1.6.0，fastapi 被拖垮
- **根因**：两个依赖对 starlette 大版本的要求互斥（fastapi 0.128 锁 <0.51，instrumentator 8.x 锁 >=1.0），pip 按后装的包解析，破坏了先装的
- **解决**：instrumentator 降到 `7.1.0`（兼容 starlette 0.x），starlette 回落 0.50.0，`pip check` 转绿；requirements.txt 加 `<8.0.0` 上限并注释原因
- **经验**：① **装新依赖后先 `pip check` 再启动**，比看报错栈快得多——报错栈指到的往往是受害者不是肇事者；② 依赖冲突在排障中的优先级要前置：功能没动过却起不来，先怀疑环境/版本，再怀疑代码；③ requirements 里发现过的冲突要留上限约束 + 注释，否则下次有人 `pip install -U` 又踩一遍



### 001 范围决策：以简历证据链为唯一范围基准（2026-08-09 · v2 开工前）

- **背景**：v2 PRD 覆盖了从单机优化到分库分表/单元化/多活的完整演进叙事，但项目根本目的是面试，简历只承载七条描述
- **决策**：以简历七条为范围基准重裁剪 todo（见 `document/todo_v2.md` 各组标注）：砍掉 Go 网关（设计文档与 demo）、分库分表/单元化/多活设计文档；结构化日志、并发测试、ADR 数量降级为最小必需集
- **经验**：① 简历项目的范围应围绕"还差什么证据"来砍——简历没写的做了是自我感动，简历写了的没证据是定时炸弹；② 砍范围时做全文档一致性扫描（PRD/todo/AGENTS 三处同步），留一处矛盾就是面试被追问的坑；③ "Design Only"类内容不删除而是标注归档，未来真有需要时不用重新调研
