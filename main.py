# -*- coding: utf-8 -*-
"""KiraAI Token Stats — Token 用量统计看板插件

为 KiraAI 提供完整的 Token 用量统计能力（移植自 Alife 的 1chuxin.TokenStats 4.9.x 设计，
并整合 KiraAI-plugin-api-balance 的查询模式）：

- 逐轮采集：@on.llm_response 钩子记录每轮 LLM 调用的 输入/输出/缓存 tokens，
  包含工具中间步；日志 JSONL 持久化到插件数据目录，重启不丢
- 费用估算：价格规则按 URL > 模型 > 渠道名 加权匹配（4/2/1 分），
  峰谷价（工作日 9:00-12:00 / 14:00-18:00 为峰，其余谷）；
  双币种（¥ 元 / 积分）分别累计，永不混算
- 余额监测：官方平台（DeepSeek/Kimi/硅基/智谱）+ One-API 系中转站 + New-API + 自定义接口，
  支持估算型（preset/daily/rolling）无接口渠道
- AI 查询函数：query_token_usage（维度聚合）与 query_token_records（逐轮明细），
  输出带 4000 字符硬上限防止回注结果撑爆上下文
- 热读缓存：按 mtime+length 判失效，大日志下轮询/查询不重复全量读盘
- 错误统计：LLM 响应内「出错：」正则扫描 + 后台日志（log.log）ERROR 行增量扫描（分类聚合：XML解析/模型调用/工具执行/网络超时/异常堆栈）+ 工具结果失败钩子（error/权限denied/超时/调用失败——LLM 白烧 token 的典型），按范围聚合

模型无关：统计基于 LLMResponse 的 input_tokens/output_tokens/cached_tokens 字段，
任何 Provider 只要上报 tokens 即可统计。
"""