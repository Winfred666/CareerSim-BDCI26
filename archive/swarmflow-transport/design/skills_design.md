# SkillSwarm 设计

当前协议见 [五组问卷工作流](ons-questionnaire-workflow.md)。

- Leader calling：observe → 五路 analyse → decide → review；运行层阻塞等待、全程静默。
- O/N/S/HW/R 五个独立分析上下文；H/W 共用，D 不翻译。
- ready/answer 是唯一答题输入，事件、游标与结果由脚本维护。
- 每事件绑定 policy 和脚本摘要；每组原子完成凭据与全部 worker 结束共同构成屏障。
- decide 只接收六指标数值、选项编号及提示；一次行动，review 后才能进入下一事件。
- 晋升 S/O、S/N 只按当前 Level 固定查表，封顶不改比例。
- 模型保持 deepseek-flash；benchmark 使用直接 API 的独立上下文，避免一般团队提示开销。

八角色实测、20题同题比较、token 与已知限制见
[questionnaire-integration-20260929.md](questionnaire-integration-20260929.md)。
