# -*- coding: utf-8 -*-
"""
Level 1 + Threading: Background Daemon Watcher Example
This example shows how to run yyds-fswatch inside a background thread in a class
(e.g., inside plugins, web applications, or long-running daemons) to handle 
events asynchronously without blocking the main application.
"""

import os
import time
import json
import threading
from yyds_fswatch import FSWatcher

class FileWatcherPlugin:
    def __init__(self, file_path):
        self.file_path = os.path.abspath(file_path)
        self.watcher = None
        self.watch_thread = None

    def start_watch(self):
        """启动 yyds-fswatch 后台守护线程监听"""
        if hasattr(self, 'watcher') and self.watcher and self.watcher.is_ready:
            print("yyds-fswatch is already running.")
            return

        watch_dir = os.path.dirname(self.file_path)
        
        # 实例化 watcher，仅监控当前目录（不递归），防抖 50ms
        self.watcher = FSWatcher(watch_dir, recursive=False, debounce=0.05)

        # 监听任何事件（created/modified/moved），确保 100% 捕获
        @self.watcher.on('*')
        def on_fs_event(event):
            # 兼容处理：源路径是 file_path，或者重命名后的目标路径是 file_path
            is_target = event.src_path == self.file_path or \
                        (event.event_type == 'moved' and event.dest_path == self.file_path)
            
            # 排除清空文件本身产生的修改（只有当文件大小大于 0 时才处理，避免二次触发）
            if is_target:
                try:
                    if os.path.exists(self.file_path) and os.path.getsize(self.file_path) > 0:
                        self.handle_message()
                except Exception as e:
                    print(f"检查文件大小时出错: {e}")

        # 使用后台守护线程非阻塞运行
        self.watch_thread = threading.Thread(target=self.watcher.start)
        self.watch_thread.daemon = True
        self.watch_thread.start()
        print("yyds-fswatch started in background thread.")

    def stop_watch(self):
        """停止 yyds-fswatch"""
        if hasattr(self, 'watcher') and self.watcher and self.watcher.is_ready:
            # 优雅通知 asyncio 事件循环退出
            if self.watcher._loop:
                from yyds_fswatch.watcher import _STOP_SENTINEL
                self.watcher._loop.call_soon_threadsafe(
                    lambda: self.watcher._queue.put_nowait(_STOP_SENTINEL)
                )
            self.watch_thread.join(timeout=1.0)
            print("yyds-fswatch stopped.")
        else:
            print("yyds-fswatch is not running.")

    def handle_message(self):
        """读取文件数据，模拟发送消息，然后回写清空"""
        try:
            with open(self.file_path, 'r', encoding='utf-8') as file:
                data_str = file.read().strip()
                if not data_str:
                    return
                data = json.loads(data_str)

            print(f"📢 [Plugin] Successfully processed data: {data}")

            # 模拟发送消息
            # print(f"Sending message: {data['message']}")

            # 批量发送完后，清空文件 (这本会触发 modified，但我们通过大小 > 0 过滤掉了)
            with open(self.file_path, 'w', encoding='utf-8') as file:
                file.write('')
            print("🧹 [Plugin] Cleaned data.json contents.")

        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"读取文件 {self.file_path} 出错: {e}")


# --- 模拟运行与测试 ---
def simulate_writer(file_path):
    time.sleep(1.0)
    print("\n--- [Writer Simulation] Writing message directly ---")
    data = {"receiver_name": "Alice", "message": "Hello from yyds-fswatch!", "group_name": ""}
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    time.sleep(1.0)

    print("\n--- [Writer Simulation] Atomic Write (Write Temp -> Rename) ---")
    temp_path = file_path + ".tmp"
    data_atomic = {"receiver_name": "Bob", "message": "Atomic save works 100%!", "group_name": "Group A"}
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data_atomic, f)
    
    # Rename causes 'moved' event, yyds-fswatch detects it successfully!
    os.rename(temp_path, file_path)
    time.sleep(1.0)

def main():
    target_file = "./data.json"
    
    # Ensure file exists at start
    with open(target_file, "w", encoding="utf-8") as f:
        f.write("")

    plugin = FileWatcherPlugin(target_file)
    plugin.start_watch()

    # Start writer thread
    writer_thread = threading.Thread(target=simulate_writer, args=(target_file,))
    writer_thread.start()

    writer_thread.join()
    time.sleep(0.5)

    plugin.stop_watch()

    # Cleanup
    if os.path.exists(target_file):
        os.remove(target_file)

if __name__ == "__main__":
    main()
