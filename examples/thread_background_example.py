# -*- coding: utf-8 -*-
"""
第一层 + 多线程：后台守护线程监听示例
该示例展示了如何在类中（如插件、Web 应用或长期运行的后台服务）
启动一个后台守护线程来运行 yyds-fswatch，实现非阻塞的异步事件处理。
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
            print("yyds-fswatch 已经在运行中。")
            return

        # 实例化 watcher，直接监控特定文件，防抖 50ms。
        # yyds-fswatch 会自动在其父目录下监听并精准过滤只放行该目标文件的事件！
        self.watcher = FSWatcher(self.file_path, recursive=False, debounce=0.05)

        # 监听任何针对该文件的事件（created/modified/moved），且保证 100% 捕获
        @self.watcher.on('*')
        def on_fs_event(event):
            # 排除清空文件本身产生的修改（只有当文件大小大于 0 时才处理，避免二次触发）
            try:
                if os.path.exists(self.file_path) and os.path.getsize(self.file_path) > 0:
                    self.handle_message()
            except Exception as e:
                print(f"检查文件大小时出错: {e}")

        # 使用后台守护线程非阻塞运行
        self.watch_thread = threading.Thread(target=self.watcher.start)
        self.watch_thread.daemon = True
        self.watch_thread.start()
        self.watcher.wait_until_ready(timeout=5.0)
        print("yyds-fswatch 已在后台守护线程中启动。")

    def stop_watch(self):
        """停止 yyds-fswatch"""
        if hasattr(self, 'watcher') and self.watcher and self.watcher.is_ready:
            self.watcher.stop()
            self.watch_thread.join(timeout=1.0)
            print("yyds-fswatch 已停止。")
        else:
            print("yyds-fswatch 并未运行。")

    def handle_message(self):
        """读取文件数据，模拟发送消息，然后回写清空"""
        try:
            with open(self.file_path, 'r', encoding='utf-8') as file:
                data_str = file.read().strip()
                if not data_str:
                    return
                data = json.loads(data_str)

            print(f"📢 [插件] 成功处理数据: {data}")

            # 模拟发送消息
            # print(f"发送消息: {data['message']}")

            # 批量发送完后，清空文件 (这本会触发 modified，但我们通过大小 > 0 过滤掉了)
            with open(self.file_path, 'w', encoding='utf-8') as file:
                file.write('')
            print("🧹 [插件] 已清空 data.json 内容。")

        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"读取文件 {self.file_path} 出错: {e}")


# --- 模拟运行与测试 ---
def simulate_writer(file_path):
    time.sleep(1.0)
    print("\n--- [模拟写入] 开始直接写入消息 ---")
    data = {"receiver_name": "Alice", "message": "来自 yyds-fswatch 的问候！", "group_name": ""}
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    time.sleep(1.0)

    print("\n--- [模拟写入] 原子覆写 (写临时文件 -> 重命名覆盖) ---")
    temp_path = file_path + ".tmp"
    data_atomic = {"receiver_name": "Bob", "message": "原子保存 100% 成功！", "group_name": "Group A"}
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data_atomic, f)
    
    # 重命名触发 moved 事件，yyds-fswatch 能够成功检测
    os.rename(temp_path, file_path)
    time.sleep(1.0)

def main():
    target_file = "./data.json"
    
    # 确保测试文件在启动时已创建
    with open(target_file, "w", encoding="utf-8") as f:
        f.write("")

    plugin = FileWatcherPlugin(target_file)
    plugin.start_watch()

    # 启动模拟写入线程
    writer_thread = threading.Thread(target=simulate_writer, args=(target_file,))
    writer_thread.start()

    writer_thread.join()
    time.sleep(0.5)

    plugin.stop_watch()

    # 清理临时文件
    if os.path.exists(target_file):
        os.remove(target_file)

if __name__ == "__main__":
    main()
