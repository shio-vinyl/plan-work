#!/usr/bin/env python3
"""全国政务文件、规划政策、公文与标准规范的检索、校验抓取与取证归档工具。

统一用法（兼容 python3 -I 隔离模式）：
  python3 -I scripts/govdoc.py --help
  python3 -I scripts/govdoc.py central "城市更新"
  python3 -I scripts/govdoc.py std "国土空间" --kind hb,db
  python3 -I scripts/govdoc.py search "土地功能混合" --track benchmark --region 北京,上海,广州
  python3 -I scripts/govdoc.py list "<栏目URL>" --pages 3 --match "规划|公告"
  python3 -I scripts/govdoc.py fetch "<URL>" --archive ./原始资料/网上公开资料/政策核查 --with-images
  python3 -I scripts/govdoc.py extract <本地OFD/PDF/WPS/ET/RAR>
"""
import os
import sys

sys.dont_write_bytecode = True

_SCRIPTS_DIR = os.path.dirname(os.path.realpath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from _govdoc.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
