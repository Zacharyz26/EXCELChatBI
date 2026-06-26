"""pytest 配置：确保项目根在 sys.path（顶层包 apps/mcp_servers/packages 可导入）。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
