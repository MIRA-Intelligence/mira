[2026-04-19 15:19] 用户请求修复 custom 配置模块在缺少 apiBase 字段时未提示用户的问题。我分析了项目结构，定位到 mira/providers/factory.py 和 mira/cli/onboard.py。修复方案：1) 在 make_provider 函数中添加验证，当 custom provider 缺少 apiBase 时抛出明确错误；2) 在 onboard 流程的 _configure_provider 函数中为 custom provider 添加 apiBase 输入提示；3) 在 tests/providers/test_factory.py 中添加测试用例验证该行为。由于环境缺少 pytest，暂未运行测试。

[2026-04-19 15:19] 用户请求修复 custom 配置模块在缺少 apiBase 字段时没有主动提示用户输入的问题。我分析了项目结构，定位到 mira/providers/factory.py 和 mira/cli/onboard.py 两个关键文件。

[2026-04-19 15:19] 在 make_provider() 函数中添加了验证逻辑：当 custom provider 的 apiBase 缺失时，抛出 ValueError 明确提示用户配置。

[2026-04-19 15:19] 在 onboard.py 的 _configure_provider() 函数中为 custom provider 添加了交互式 apiBase 输入提示流程。

[2026-04-19 15:19] 在 tests/providers/test_factory.py 中添加了 test_custom_provider_requires_api_base 测试用例。

[2026-04-19 15:27] 用户反馈测试时 custom 配置仍没有提示输入 apiBase，需要进一步排查 onboard 流程中的提示逻辑是否正确触发。

