# Long-term Memory

## User Information

(Important facts about the user)

## Preferences

(User preferences learned over time)

## Project Context

**Current Project**: Mira - 医学影像研究助手
**Key Files Modified**:
- `mira/providers/factory.py`: 添加了 custom provider apiBase 缺失时的验证逻辑，抛出 ValueError 提示用户配置
- `mira/cli/onboard.py`: 在 _configure_provider 函数中为 custom provider 添加交互式 apiBase 输入提示
- `tests/providers/test_factory.py`: 新增 test_custom_provider_requires_api_base 测试用例

**Architecture Notes**:
- 配置使用 Pydantic schema (`mira/config/schema.py`)
- Provider 创建通过 `make_provider` factory 函数
- Onboard 流程在 `mira/cli/onboard.py` 中处理交互式配置
- Custom provider 默认回退到 `http://localhost:8000/v1`，但修复后会在缺失时主动提示

## Important Notes

- 测试环境缺少 pytest 模块，需要使用项目指定的虚拟环境运行测试
- Python 路径: /usr/bin/python3
- 项目根目录: /homes/yqyi/Code/GitCode/Mira/Mira
- 用户反馈 custom 配置时仍未提示输入 apiBase，需检查 onboard 流程中 custom provider 的提示逻辑是否正确触发