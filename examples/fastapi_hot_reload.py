# -*- coding: utf-8 -*-
"""
FastAPI + yyds-fswatch：配置/数据热加载至内存示例
该示例展示了如何在 FastAPI 应用中，利用 yyds-fswatch 的异步迭代器，
在主 asyncio 事件循环中非阻塞地监听配置文件，并实现热加载至内存。
"""

import os
import json
import asyncio
from contextlib import asynccontextmanager

# 尝试导入 fastapi，如果未安装则给予提示
try:
    from fastapi import FastAPI
except ImportError:
    print("提示: 运行此示例需要安装 fastapi 和 uvicorn。可以使用以下命令安装:")
    print("pip install fastapi uvicorn")
    import sys
    sys.exit(0)

# 1. 内存中的全局数据缓存
DATA_CACHE = {
    "config": {}
}

CONFIG_FILE = "config.json"

def reload_config_to_memory():
    """将物理文件中的数据安全读取并加载到内存"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    DATA_CACHE["config"] = json.loads(content)
                    print(f"🔄 [内存热更新] 成功加载最新配置: {DATA_CACHE['config']}")
        except Exception as e:
            print(f"❌ 读取配置文件失败: {e}")

# 2. 完美的 FastAPI Lifespan 生命周期管理
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时：先首次加载一次数据到内存
    reload_config_to_memory()

    # 导入 yyds-fswatch
    from yyds_fswatch import FSWatcher

    # 启动时：创建一个 asyncio 协程任务在后台监听文件变更
    watcher = FSWatcher(CONFIG_FILE, recursive=False, debounce=0.05)
    
    async def config_watcher_task():
        # Linux 原生后端直接绑定 asyncio loop；其他后端会安全桥接 OS 等待线程。
        async with watcher as w:
            async for event in w:
                print(f"检测到文件变更, {event}")
                # 排除文件被意外删除等异常，只有文件存在且有内容时才加载
                if os.path.exists(CONFIG_FILE) and os.path.getsize(CONFIG_FILE) > 0:
                    reload_config_to_memory()

    # 将监听协程作为后台 Task 运行
    bg_task = asyncio.create_task(config_watcher_task())
    
    yield  # 这里是 FastAPI 开始接收外部 HTTP 请求的阶段

    # 关闭时：优雅取消后台任务，防内存和协程泄露
    bg_task.cancel()
    try:
        await bg_task
    except asyncio.CancelledError:
        pass
    print("🔌 监听任务已安全关闭。")

# 3. 创建 FastAPI App，注入生命周期管理器
app = FastAPI(lifespan=lifespan)

# 4. HTTP API 接口
@app.get("/config")
async def get_config():
    # 瞬间从内存读取并直接返回，无任何文件 I/O 阻塞，并发吞吐量极高
    return {
        "status": "success",
        "data": DATA_CACHE["config"]
    }

# 5. 提供简单的自测试模拟
async def simulate_file_writes():
    await asyncio.sleep(2.0)
    print("\n--- [模拟写入] 写入配置 v1 ---")
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"site_name": "我的网站", "debug": True}, f)
        
    await asyncio.sleep(2.0)
    print("\n--- [模拟写入] 写入配置 v2 (原子替换方式) ---")
    temp_file = CONFIG_FILE + ".tmp"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump({"site_name": "我的网站v2", "debug": False, "version": "1.0.2"}, f)
    os.rename(temp_file, CONFIG_FILE)
    
    await asyncio.sleep(2.0)

if __name__ == "__main__":
    import uvicorn
    
    # 确保测试文件在启动时已创建
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"site_name": "初始网站", "debug": False}, f)
        
    print("正启动 FastAPI 并在后台启动模拟写入测试，稍后将自动退出...")
    
    # 启动文件写入模拟任务
    loop = asyncio.get_event_loop()
    write_task = loop.create_task(simulate_file_writes())
    
    # 启动 uvicorn
    config = uvicorn.Config(app=app, host="127.0.0.1", port=8000, log_level="warning")
    server = uvicorn.Server(config)
    
    # 运行 7 秒后通过优雅停止 uvicorn 实例退出服务
    loop.call_later(7.0, lambda: setattr(server, 'should_exit', True))
    
    # 在同一个事件循环中启动 FastAPI 服务
    try:
        loop.run_until_complete(server.serve())
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        # 清理临时文件
        if os.path.exists(CONFIG_FILE):
            os.remove(CONFIG_FILE)
        print("示例已结束。")
