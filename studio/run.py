"""启动上刊素材工作台：python studio/run.py （默认 http://localhost:8600）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dibiao_studio.server import main  # noqa: E402

if __name__ == "__main__":
    main()
