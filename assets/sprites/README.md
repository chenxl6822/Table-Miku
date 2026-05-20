# Miku Sprite Assets

把透明背景的 chibi Miku PNG 放在这个目录中：

- `miku_sprite_sheet.png`：推荐，AI 生成的五表情横向精灵图，顺序为 idle、focus、happy、surprised、sleepy。
- `miku_idle.png`：默认表情，必须优先提供。
- `miku_happy.png`：开心表情，可选。
- `miku_focus.png`：专注表情，可选。
- `miku_surprised.png`：惊讶表情，可选。
- `miku_sleepy.png`：困困表情，可选。

如果提供 `miku_sprite_sheet.png`，程序会自动裁成 5 个表情。若图片带棋盘格假透明背景，程序会尽量从边缘去除背景。

如果只提供 `miku_idle.png`，程序会用同一张图片搭配爱心、惊讶线、Zzz 等小效果来模拟表情变化。

建议素材：

- 透明 PNG。
- 角色比例接近正方形。
- 粗描边、青绿色头发、大眼高光、粉色腮红。
- 主体不要太大，留出下方键盘区域。

请确认素材授权后再转发或发布。
