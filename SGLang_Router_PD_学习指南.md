# SGLang Router PD 分离架构学习指南

## 📚 学习路径

### 第一阶段：快速入门（1-2小时）

#### 1. 核心概念理解

**什么是 PD 分离？**
- **Prefill（预填充）**：计算密集型阶段，处理整个输入序列
- **Decode（解码）**：内存密集型阶段，管理 KV Cache 进行 token 生成
- **传统问题**：统一调度导致 Prefill 中断 Decode，造成延迟

**PD 分离的优势：**
1. **消除 Prefill 中断**：Decode 不再被 Prefill 打断
2. **DP Attention 平衡**：避免一个 DP worker 处理 Prefill 时另一个处理 Decode 的不平衡
3. **独立优化**：针对计算密集和内存密集分别优化

#### 2. 快速启动示例

**单节点 Llama 模型（最简单）：**

```bash
# 启动 Prefill 服务器（GPU 0）
python -m sglang.launch_server \
  --model-path meta-llama/Llama-3.1-8B-Instruct \
  --disaggregation-mode prefill \
  --port 30000 \
  --disaggregation-ib-device mlx5_roce0

# 启动 Decode 服务器（GPU 1）
python -m sglang.launch_server \
  --model-path meta-llama/Llama-3.1-8B-Instruct \
  --disaggregation-mode decode \
  --port 30001 \
  --base-gpu-id 1 \
  --disaggregation-ib-device mlx5_roce0

# 启动 Router（负责协调）
python -m sglang_router.launch_router \
  --pd-disaggregation \
  --prefill http://127.0.0.1:30000 \
  --decode http://127.0.0.1:30001 \
  --host 0.0.0.0 \
  --port 8000
```

**测试请求：**
```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "meta-llama/Llama-3.1-8B-Instruct",
    "messages": [{"role": "user", "content": "Hello!"}],
    "max_tokens": 100
  }'
```

---

### 第二阶段：架构深入（2-4小时）

#### 1. Router 工作原理

**Router 的三大职责：**

1. **请求路由**
   - 接收客户端请求
   - 将请求发送到 Prefill 服务器
   - 等待 Prefill 完成并获取 KV Cache 元数据

2. **KV Cache 传输协调**
   - Prefill 服务器通过高速网络（IB/RDMA）将 KV Cache 传输到 Decode 服务器
   - 支持两种传输引擎：
     - **Mooncake**：推荐用于 NVL72 部署，支持 NVLink
     - **NIXL**：基于 UCX/LibFabric 的通用方案

3. **响应合并**
   - 从 Decode 服务器接收生成的 tokens
   - 流式返回给客户端

**数据流图：**
```
客户端请求
    ↓
Router (接收)
    ↓
Prefill 服务器 (处理输入序列)
    ↓
KV Cache 传输 (Mooncake/NIXL)
    ↓
Decode 服务器 (生成 tokens)
    ↓
Router (合并响应)
    ↓
客户端响应 (流式)
```

#### 2. 负载均衡策略

Router 支持多种策略：

| 策略 | 适用场景 | 说明 |
|------|---------|------|
| `cache_aware` | 重复请求多 | 维护前缀树，路由相似请求到同一 worker |
| `power_of_two` | 负载不均 | 从两个随机 worker 中选择负载较轻的 |
| `round_robin` | 均匀分布 | 顺序轮询 |
| `random` | 简单场景 | 随机选择 |

**配置示例：**
```bash
python -m sglang_router.launch_router \
  --pd-disaggregation \
  --prefill http://p1:30000 http://p2:30000 \
  --decode http://d1:30001 http://d2:30001 \
  --policy cache_aware \
  --prefill-policy cache_aware \
  --decode-policy power_of_two
```

#### 3. 传输引擎对比

**Mooncake（推荐）：**
- ✅ 支持 NVLink（NVL72 部署）
- ✅ 自定义内存池优化
- ✅ 多队列并行传输
- ⚠️ 需要安装：`pip install mooncake-transfer-engine`

**NIXL：**
- ✅ 基于 UCX/LibFabric，通用性强
- ✅ 支持多种后端（UCX、LibFabric）
- ⚠️ 需要安装：`pip install nixl`

**环境变量配置（Mooncake）：**
```bash
# 启用 NVLink 传输（NVL72 推荐）
export SGLANG_MOONCAKE_CUSTOM_MEM_POOL=NVLINK
export MC_FORCE_MNNVL=True

# 调整线程池大小（默认自动计算）
export SGLANG_DISAGGREGATION_THREAD_POOL_SIZE=8

# 调整队列大小（默认 4）
export SGLANG_DISAGGREGATION_QUEUE_SIZE=4

# 超时配置（默认 300 秒）
export SGLANG_DISAGGREGATION_BOOTSTRAP_TIMEOUT=600
export SGLANG_DISAGGREGATION_WAITING_TIMEOUT=600
```

---

### 第三阶段：生产部署（4-8小时）

#### 1. 多节点 DeepSeek 部署

**Prefill 节点 0：**
```bash
python -m sglang.launch_server \
  --model-path deepseek-ai/DeepSeek-V3-0324 \
  --disaggregation-ib-device mlx5_roce0 \
  --disaggregation-mode prefill \
  --host 10.20.32.68 \
  --port 30000 \
  --trust-remote-code \
  --dist-init-addr 10.20.32.68:5000 \
  --nnodes 2 \
  --node-rank 0 \
  --tp-size 16 \
  --dp-size 8 \
  --enable-dp-attention \
  --moe-a2a-backend deepep \
  --mem-fraction-static 0.8
```

**Prefill 节点 1：**
```bash
python -m sglang.launch_server \
  --model-path deepseek-ai/DeepSeek-V3-0324 \
  --disaggregation-ib-device mlx5_roce0 \
  --disaggregation-mode prefill \
  --host 10.20.32.69 \
  --port 30000 \
  --trust-remote-code \
  --dist-init-addr 10.20.32.68:5000 \
  --nnodes 2 \
  --node-rank 1 \
  --tp-size 16 \
  --dp-size 8 \
  --enable-dp-attention \
  --moe-a2a-backend deepep \
  --mem-fraction-static 0.8
```

**Decode 节点 0 & 1：**（类似配置，改为 `--disaggregation-mode decode`）

**Router：**
```bash
python -m sglang_router.launch_router \
  --pd-disaggregation \
  --prefill http://10.20.32.68:30000 \
  --prefill http://10.20.32.69:30000 \
  --decode http://10.20.32.70:30001 \
  --decode http://10.20.32.71:30001 \
  --host 0.0.0.0 \
  --port 8000 \
  --policy cache_aware
```

#### 2. 可靠性配置

**重试机制：**
```bash
--retry-max-retries 5 \
--retry-initial-backoff-ms 100 \
--retry-max-backoff-ms 10000 \
--retry-backoff-multiplier 2.0 \
--retry-jitter-factor 0.1
```

**熔断器：**
```bash
--cb-failure-threshold 5 \
--cb-success-threshold 2 \
--cb-timeout-duration-secs 60 \
--cb-window-duration-secs 10
```

**限流：**
```bash
--max-concurrent-requests 512 \
--rate-limit-tokens-per-second 1000 \
--queue-size 1024 \
--queue-timeout-secs 30
```

#### 3. 监控与可观测性

**Prometheus 指标（40+ 指标）：**
```bash
--prometheus-host 0.0.0.0 \
--prometheus-port 29000
```

**关键指标：**
- `smg_router_ttft_seconds` - 首 token 延迟（TTFT）
- `smg_router_tpot_seconds` - 每 token 延迟（TPOT）
- `smg_router_tokens_total` - 总 token 数
- `smg_worker_cb_state` - 熔断器状态
- `smg_worker_retries_total` - 重试次数

**OpenTelemetry 追踪：**
```bash
--enable-trace \
--otlp-traces-endpoint localhost:4317
```

**日志配置：**
```bash
--log-level info \
--log-dir /var/log/sglang-router
```

---

### 第四阶段：底层机制（深入源码）

#### 1. 核心代码路径

**Router 实现：**
```
sgl-model-gateway/src/routers/
├── http/
│   ├── pd_router.rs          # HTTP PD 路由器
│   └── pd_types.rs           # PD 类型定义
├── grpc/
│   └── pd_router.rs          # gRPC PD 路由器
└── factory.rs                # 路由器工厂
```

**Python 绑定：**
```
sgl-model-gateway/bindings/python/src/sglang_router/
├── launch_router.py          # Router 启动器
├── router.py                 # Python Router 接口
└── router_args.py            # 参数解析
```

**Disaggregation 核心：**
```
python/sglang/srt/disaggregation/
├── prefill.py                # Prefill 服务器逻辑
├── decode.py                 # Decode 服务器逻辑
├── common/conn.py            # 连接管理
├── mooncake/conn.py          # Mooncake 传输
└── nixl/conn.py              # NIXL 传输
```

#### 2. 关键流程源码分析

**Prefill 服务器初始化：**
```python
# python/sglang/srt/disaggregation/prefill.py
class PrefillScheduler:
    def __init__(self):
        # 初始化传输引擎
        self.transfer_engine = create_transfer_engine(backend)
        # 注册到 Router
        self.register_to_router()
    
    def forward_prefill(self, batch):
        # 1. 执行 Prefill 计算
        kv_cache = self.model.forward(batch)
        # 2. 传输 KV Cache 到 Decode
        self.transfer_engine.send_kv_cache(kv_cache, decode_addr)
        # 3. 返回元数据给 Router
        return metadata
```

**Decode 服务器接收：**
```python
# python/sglang/srt/disaggregation/decode.py
class DecodeScheduler:
    def receive_kv_cache(self, metadata):
        # 1. 等待 KV Cache 传输完成
        kv_cache = self.transfer_engine.recv_kv_cache(metadata)
        # 2. 开始 Decode
        tokens = self.model.decode(kv_cache)
        # 3. 流式返回给 Router
        return tokens
```

**Router 协调：**
```rust
// sgl-model-gateway/src/routers/http/pd_router.rs
async fn handle_pd_request(req: Request) -> Response {
    // 1. 选择 Prefill worker
    let prefill_worker = policy.select_prefill_worker();
    
    // 2. 发送到 Prefill
    let metadata = prefill_worker.forward(req).await?;
    
    // 3. 选择 Decode worker
    let decode_worker = policy.select_decode_worker();
    
    // 4. 等待 Decode 完成
    let stream = decode_worker.decode(metadata).await?;
    
    // 5. 流式返回
    Ok(Response::stream(stream))
}
```

#### 3. KV Cache 传输机制

**Mooncake 传输流程：**
```python
# python/sglang/srt/disaggregation/mooncake/conn.py
class MooncakeConnection:
    def send_kv_cache(self, kv_cache, dest_addr):
        # 1. 分配传输缓冲区
        buffer = self.allocate_buffer(kv_cache.size)
        
        # 2. 使用 RDMA/NVLink 传输
        self.rdma_write(buffer, dest_addr)
        
        # 3. 发送完成通知
        self.send_completion_signal(dest_addr)
```

**NIXL 传输流程：**
```python
# python/sglang/srt/disaggregation/nixl/conn.py
class NIXLConnection:
    def send_kv_cache(self, kv_cache, dest_addr):
        # 1. 使用 UCX/LibFabric
        self.ucx_client.send(kv_cache, dest_addr)
        
        # 2. 等待 ACK
        self.wait_for_ack()
```

---

## 🔍 故障排查

### 常见问题

**1. KV Cache 传输超时**
```bash
# 症状：TTFT 很高，日志显示传输超时
# 解决：增加超时时间
export SGLANG_DISAGGREGATION_WAITING_TIMEOUT=600
export SGLANG_DISAGGREGATION_BOOTSTRAP_TIMEOUT=600
```

**2. Prefill/Decode 不匹配**
```bash
# 症状：Router 报错 "No decode worker available"
# 解决：确保 Prefill 和 Decode 使用相同的模型和配置
# 检查：
curl http://prefill-server:30000/get_model_info
curl http://decode-server:30001/get_model_info
```

**3. 网络带宽不足**
```bash
# 症状：传输慢，GPU 利用率低
# 解决：
# - 使用 InfiniBand/RoCE 网络
# - 启用 NVLink（NVL72）
export SGLANG_MOONCAKE_CUSTOM_MEM_POOL=NVLINK
export MC_FORCE_MNNVL=True
```

**4. 内存不足**
```bash
# 症状：OOM 错误
# 解决：调整内存分配
--mem-fraction-static 0.8  # 降低静态内存占用
--max-running-requests 64  # 限制并发请求
```

---

## 📖 推荐阅读顺序

### 文档路径
1. **快速入门**：`docs/advanced_features/pd_disaggregation.md`
2. **Router 详细文档**：`sgl-model-gateway/README.md`
3. **性能优化博客**：
   - [Large-scale EP](https://lmsys.org/blog/2025-05-05-large-scale-ep/)
   - [GB200 Part 1](https://lmsys.org/blog/2025-06-16-gb200-part-1/)
   - [GB200 Part 2](https://lmsys.org/blog/2025-09-25-gb200-part-2/)

### 源码阅读顺序
1. **Router 入口**：`sgl-model-gateway/bindings/python/src/sglang_router/launch_router.py`
2. **PD 路由器**：`sgl-model-gateway/src/routers/http/pd_router.rs`
3. **Prefill 调度器**：`python/sglang/srt/disaggregation/prefill.py`
4. **Decode 调度器**：`python/sglang/srt/disaggregation/decode.py`
5. **传输引擎**：`python/sglang/srt/disaggregation/mooncake/conn.py`

---

## 🎯 实战练习

### 练习 1：单机部署
在单台机器上部署 PD 分离架构，测试基本功能。

### 练习 2：多节点部署
在多台机器上部署 DeepSeek-V3，配置 TP=16, DP=8。

### 练习 3：性能调优
使用 Prometheus 监控，优化 TTFT 和吞吐量。

### 练习 4：故障注入
模拟 Prefill 服务器故障，观察 Router 的熔断和重试行为。

---

## 📞 获取帮助

- **官方文档**：https://docs.sglang.io/
- **GitHub Issues**：https://github.com/sgl-project/sglang/issues
- **Slack 社区**：https://slack.sglang.io/
- **每周开发会议**：https://meet.sglang.io/

---

**最后更新**：2026-03-10
