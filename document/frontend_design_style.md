# Frontend Design Style Guide - AI Avalon

## 1. 概述 (Overview)
本文档定义了 Aivalon 项目前端页面的设计风格。
**核心理念**：**中世纪奇幻 (Medieval Fantasy) 与 魔法秘术 (Arcane Magic)**。
目标是构建一个充满神秘感、史诗感且沉浸的“阿瓦隆”桌游体验。界面应减少现代科技感（如扁平化纯色），增加质感（如金属、羊皮纸、光晕）。

## 2. 配色方案 (Color Palette)

### 2.1 基础色 (Base Colors)
- **Background (Main)**: `#0f1016` (深邃夜空黑，略带蓝紫调，模拟深夜圆桌会议)
- **Background (Card/Panel)**: `#1a1b26` (深色底板) 配合 `rgba(20, 20, 30, 0.85)` 半透明磨砂
- **Text (Primary)**: `#e2e8f0` (月光银白，主要文字)
- **Text (Secondary)**: `#94a3b8` (秘银灰，次要文字)
- **Text (Accent)**: `#fbbf24` (古金，用于强调、标题、关键数值)
- **Border**: `#475569` (普通边框) 或 `#b45309` (暗金边框，用于高亮)

### 2.2 品牌与功能色 (Brand & Functional Colors)
- **Primary (Magic/Arcane)**: `#7c3aed` (秘法紫) - 用于核心交互、确认按钮（带有魔法流动感）。
- **Highlight (Gold)**: `#f59e0b` (琥珀金) - 用于皇冠、队长标识、胜利结算。
- **Success (Good/Loyal)**: `#0ea5e9` (圣光蓝) - 代表亚瑟王的忠诚卫士（比纯绿更具神圣感）。
- **Danger (Evil/Minion)**: `#dc2626` (鲜血红) - 代表莫德雷德的爪牙。
- **Warning**: `#d97706` (深橙) - 警告、倒计时。

### 2.3 阵营色 (Faction Colors)
- **Good (Servants of Arthur)**: `#38bdf8` (Sky Blue) 配合 银色边框。
- **Evil (Minions of Mordred)**: `#ef4444` (Crimson Red) 配合 暗紫/黑色边框。

## 3. 排版 (Typography)
结合衬线体与无衬线体，营造古籍与现代易读性的平衡。

- **Font Family**:
  - **Headings (Title/Role)**: `'Cinzel', 'Playfair Display', serif` (具有古典雕刻感的衬线体)
  - **Body (Content/Chat)**: `'Inter', system-ui, sans-serif` (保持正文清晰易读)
- **Styles**:
  - H1: 36px, Gold Gradient (金色渐变填充)
  - H2: 24px, Serif
  - Label: 12px, Uppercase, Letter-spacing 1px (增加仪式感)

## 4. 视觉材质与特效 (Visual Textures & Effects)
- **Glassmorphism (Dark)**: 背景使用深色毛玻璃效果，模拟水晶球或魔法屏障。
  - `backdrop-filter: blur(12px)`
  - `background: rgba(30, 41, 59, 0.7)`
- **Glow (光晕)**: 重要元素（如选中的玩家、进行的任务）带有外发光。
  - `box-shadow: 0 0 15px rgba(124, 58, 237, 0.5)` (紫色魔法光晕)
- **Borders**:
  - 这里的边框不仅仅是 1px solid，可以是双线，或者带有 `border-image` (金属质感)。

## 5. 核心组件 (Core Components)

### 5.1 按钮 (Buttons)
- **Primary (Spell)**: 紫色渐变背景 (`linear-gradient(135deg, #6d28d9, #7c3aed)`)，白色文字，带微弱发光。
- **Action (Vote)**:
  - **Approve**: 宝石蓝/青色背景，银色边框。
  - **Reject**: 深红背景，暗纹边框。
- **Ghost**: 透明背景，金色文字，Hover 时显示金色边框。

### 5.2 卡片 (Cards)
- 深色半透明背景。
- **边框**: 细微的金属质感边框 (`#334155`)。
- **Hover**: 边框亮起（金色或紫色），仿佛被魔法选中。

### 5.3 玩家头像 (Avatars)
- **Frame**: 圆形头像，带有金属圆环（银色/金色/生锈铁色）包裹。
- **Status**: 不再是简单的圆点，可以是头像周围的符文光圈。
  - 发言中: 蓝色符文旋转。
  - 队长: 明显的金色皇冠图标叠加。

### 5.4 游戏面板 (Game Board)
- **Mission Track**:
  - 任务节点设计为“徽章”或“盾牌”形状，而非简单的圆圈。
  - 成功: 盾牌点亮（蓝色圣火）。
  - 失败: 盾牌破裂或染红（红色印记）。
- **Vote Track**:
  - 使用“圣杯”或“宝石”图标表示进度。

## 6. 交互动效 (Interactions)
- **Reveal**: 身份揭示时，使用翻牌动画，伴随魔法粒子效果。
- **Vote Result**: 投票结果公布时，依次点亮（火焰燃起或熄灭）。
- **Phase Change**: 阶段切换时，使用淡入淡出配合文字推拉（如电影过场）。

## 7. 资源建议 (Assets)
- 背景图：可以叠加一层淡淡的羊皮纸纹理或星图纹理 (`opacity: 0.05`)。
- 图标库：使用 `Lucide-vue-next` (通用) + 自定义 SVG (剑、盾、圣杯、皇冠)。
