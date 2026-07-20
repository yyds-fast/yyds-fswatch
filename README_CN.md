# 🚀 yyds-fswatch

`yyds-fswatch` 是一个面向现代 Python 的轻量级、asyncio 友好文件系统监听库。它提供比 Observer/Handler 更紧凑的 API，并让平台降级行为保持可见。

---

## 🌟 核心优势 (对比 Watchdog)

| 特性 | `yyds-fswatch` (本库) | `watchdog` |
| :--- | :--- | :--- |
| **API 易用性** | 渐进式 4 层 API（装饰器 / 回调 / 同步迭代 / 异步迭代） | 繁琐的 Observer + Handler 类继承体系 |
| **异步集成** | **原生协程支持** (`async with` 和 `async for`) | 需要自行使用线程池或 `loop.call_soon_threadsafe` 桥接 |
| **Linux 性能** | **零额外线程**，纯 ctypes 桥接非阻塞 `inotify` + `loop.add_reader` | 必须为每个 Emitter 启动一个独立的 Python OS 阻塞读取线程 |
| **原子保存兼容** | 单文件监听父目录，并把外部临时文件替换归一化为目标文件事件 | 取决于 Observer 与 Handler 配置 |
| **防抖降噪** | 内置 50ms 滑动窗口状态机，高频写入自动合并 | 无防抖，保存一个文件会瞬间触发数十个重复 Graves/modified 事件 |
| **GitIgnore 剪枝** | 动态重载嵌套 `.gitignore`，支持常用 glob 子集 | 通常需要应用层过滤 |
| **重命名同步** | Linux Cookie、Windows 事务包、FSEvents 启发式与 Polling inode 配对 | 依赖具体后端 |
| **溢出容灾** | 发出 `overflow`；Linux 同时重建递归 watch 树 | 依赖具体后端 |
| **根路径自愈** | 监听根消失或重建时重启后端 | 通常需要重建 Observer |

---

## 📦 安装

通过 pip 安装 `yyds-fswatch`：
```bash
pip install -U yyds-fswatch
```

或者在克隆项目到本地后从源码安装：
```bash
pip install .
```

---

## 🛠️ 渐进式 API 使用示例

`yyds-fswatch` 设计了四种层级的 API，满足不同场景下的快速集成。

### 1. Level 1: 装饰器模式 (Decorator Mode)
最直观、对类 and 框架开发者最友好的开发模式，支持同步或异步回调函数。
```python
from yyds_fswatch import FSWatcher

w = FSWatcher("./watch_dir", recursive=True, debounce=0.05)

@w.on("created")
def on_created(event):
    print(f"🔥 [CREATED] {'Directory' if event.is_directory else 'File'}: {event.src_path}")

@w.on("modified")
def on_modified(event):
    print(f"⚡ [MODIFIED]: {event.src_path}")

@w.on("deleted")
def on_deleted(event):
    print(f"🗑️ [DELETED]: {event.src_path}")

@w.on("moved")
def on_moved(event):
    print(f"📦 [MOVED]: {event.src_path} -> {event.dest_path}")

# 启动监听（阻塞当前线程，或在子线程中运行 w.start）
# 阻塞运行，直到 Ctrl+C 或其他线程调用 w.stop()
w.start()
```

### 2. Level 2: 一行命令回调 (One-Liner Callback)
最适合快捷脚本或临时调试，一行代码搞定。
```python
from yyds_fswatch import watch

# 阻塞监听，并在有任何变更时调用指定回调
watch("./watch_dir", on_change=lambda event: print(f"⚡ Event: {event.event_type} on {event.src_path}"))
```

### 3. Level 3: 同步标准迭代器 (Sync Iterator)
采用 `with` 上下文管理器，像读取生成器一样安全、顺序地处理文件事件。
```python
from yyds_fswatch import FSWatcher

# 在 with 块中会自动拉起后台监听线程，出块自动安全释放
with FSWatcher("./watch_dir", debounce=0.05) as w:
    for event in w:
        print(f"🔄 [Sync Iter] {event.event_type.upper()}: {event.src_path}")
        if event.event_type == "deleted" and "stop_trigger" in event.src_path:
            break  # 退出循环，watcher 会安全自动销毁
```

### 4. Level 4: 异步原生迭代器 (Async Iterator) - ✨ 推荐
适配 FastAPI、Sanic、Tornado 等异步框架。Linux 直接使用调用方事件循环；Windows/macOS 原生后端使用一个等待 OS 事件的辅助线程。
```python
import asyncio
from yyds_fswatch import FSWatcher

async def main():
    # 纯异步上下文和异步迭代器，无缝融入 asyncio 调度
    async with FSWatcher("./watch_dir", debounce=0.05) as w:
        async for event in w:
            print(f"👀 [Async Iter] {event.event_type.upper()}: {event.src_path}")

asyncio.run(main())
```

### 生命周期、背压与后端控制

```python
import threading
from yyds_fswatch import FSWatcher

w = FSWatcher(
    "./watch_dir",
    max_queue_size=4096,   # 设为 0 可选择无界队列
    backend="auto",       # auto | native | polling
    recovery_interval=2.0,
)
thread = threading.Thread(target=w.start)
thread.start()
w.wait_until_ready(timeout=5)

# 稍后安全停止
w.stop()                  # 公开、幂等、线程安全
thread.join()
```

监听尚不存在的文件时请设置 `target_type="file"`。如果需要接收 `dist`、`build`、`node_modules` 等常见生成目录内的事件，请设置 `ignore_common_dirs=False`。

---


## 🛡️ 深度技术优化细节

1. **原子保存归一化**：对 `data.json` 等单文件目标监听父目录；临时文件覆盖已有目标时，对外产生目标路径上的 `modified`。
2. **缓存路径过滤**：绝对根路径前缀判断与 4096 项有界决策缓存减少重复工作；不再使用未经基准验证的固定吞吐量数字。
3. **生命周期与内存控制**：上下文管理器和公开 `stop()` 负责释放后端资源；用户态队列默认上限 4096，消费者落后时发出 `overflow`。
4. **系统缓冲区溢出处理**：系统队列饱和时发出 `overflow`；Linux 还会重建 inotify 实例及递归 watch 树，防止永久漏掉目录注册。
5. **层级嵌套 `.gitignore`**：规则文件变化时使缓存失效；支持常用 `*`、`?`、`**`、锚定、目录和否定规则，明确定位为常用子集而非逐字节等价的 Git 实现。
6. **自愈性根目录丢失重连**：若监控的根目录被意外删除或磁盘卸载，系统会自动挂起平台监控并进入 2 秒低开销轮询重试。当根目录被重新创建，Watcher 将自动无缝恢复底层原生监听。

---

## 📈 长期运行资源控制

`yyds-fswatch` 提供适合常驻进程的资源控制；正式生产采用前仍应在每种目标操作系统和文件系统上完成验证：

* **有界队列与缓存**：事件队列和 `.gitignore` 决策缓存默认上限均为 `4096`；后端描述符映射会随实际监听目录数量增长。
* **低空闲开销**：原生驱动等待操作系统事件；轻量生命周期心跳保证跨线程及时停止，默认每 2 秒执行一次可配置的根路径健康检查，Polling 后端按配置间隔扫描。
* **状态自动销毁机制**：防抖延迟队列和 Rename Cookie 等临时挂起状态均在 50ms 窗口后自动销毁并从内存 pop 清空，空闲时状态字典长度常态为 0。
* **动态描述符剪枝与垃圾回收**：启动时根据 `.gitignore` 进行 Watch 树剪枝，规避 `ENOSPC` 描述符上限；当目录被物理删除时，捕获 `IN_IGNORED` 自动同步并释放内存映射句柄；在主动退出时，所有底层 FD 句柄均会被安全关闭。
