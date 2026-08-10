#!/usr/bin/env bash
# 一键测试入口（D 组重构期间常驻运行）
# 用法：cd backend && ./run_tests.sh
# 单元测试总是运行；集成测试需要完整环境在线（见 tests/test_game_flow_integration.py 头部说明）
set -e
cd "$(dirname "$0")"
source venv/bin/activate
python -m pytest tests/ -v "$@"
