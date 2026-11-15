# Agent Instructions

- 默认使用中文；命令、路径、标识符保持英文。
- 本仓库是通用 skill：只写这类工作的通用做法，不写死某个项目的口径和数字；项目经验只作为例子出现。
- 不引用其他插件或 skill，保持自成一体。
- 测试产物（下载、提取结果、转写稿、缓存）放仓库外的临时目录，结束后清理；Python 用 `PYTHONDONTWRITEBYTECODE=1`。
- 修改后用 skill-creator 的 `quick_validate.py` 检查 frontmatter；改了脚本要实际跑一遍。
- 不推送，除非用户明确要求。
