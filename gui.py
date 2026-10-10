import copy
import json
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

import yaml

from core import load_nodes, protocol_options, resolve_paths


def completion_summary(path):
    report = json.loads(path.with_name("results.json").read_text(encoding="utf-8"))
    rows = report["results"]
    complete = [row for row in rows if row.get("status") == "已完成"]
    failed = sum(row.get("status") == "连接失败" for row in rows)
    untested = sum(row.get("status") == "未检测" for row in rows)
    text = f"已测 {len(rows)-untested}/{len(rows)} · 完整评分 {len(complete)} · 连接失败 {failed} · 待补全 {len(rows)-len(complete)-failed-untested}"
    if complete:
        best = max(complete, key=lambda row: row["score"])
        text += f"\n完整评分节点中最高：{best['name']} · {best.get('region', '未知')} · {best['score']:.2f}/100"
    if report.get("iplark_block"):
        text += "\n" + report["iplark_block"]["message"]
    return text


def show(args):
    from checker import run
    root = tk.Tk()
    root.title("FlClash 节点综合评分")
    root.geometry("860x720")
    root.minsize(650, 450)
    messages = queue.Queue()
    cancel = threading.Event()
    running = False
    last_report = None
    frame = ttk.Frame(root, padding=22)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="FlClash · 一键节点测评", font=("Microsoft YaHei", 19, "bold")).pack(anchor="w")
    ttk.Label(frame, text="网速 20% · IPLark 40% · 请求成功率 20% · 延迟 10% · 波动 5% · 连通性 5%",
              wraplength=710).pack(anchor="w", pady=(10, 6))
    ttk.Label(frame, text="优先使用有效缓存，缺失时复用本机 CloakBrowser（9222）；未运行则使用独立 Edge。\n实时查询至少间隔 10 秒，拦截时保存冷却记录；默认每节点最多下载 16 MiB。",
              wraplength=710).pack(anchor="w")
    config_path = tk.StringVar(value=args.config or "")
    core_path = tk.StringVar(value=args.core or "")
    path_note = tk.StringVar(value="路径留空时自动识别；移动 FlClash 后请先打开它。")
    path_controls = []
    for title, variable, kind in [("配置文件：", config_path, "config"),
                                   ("FlClash 程序：", core_path, "core")]:
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(8, 0))
        ttk.Label(row, text=title, width=14).pack(side="left")
        entry = ttk.Entry(row, textvariable=variable)
        entry.pack(side="left", fill="x", expand=True)
        button = ttk.Button(row, text="选择文件", command=lambda v=variable, k=kind: choose_path(v, k))
        button.pack(side="left", padx=(8, 0))
        path_controls.extend([entry, button])
    ttk.Label(frame, textvariable=path_note, wraplength=800).pack(anchor="w", pady=(6, 0))
    controls = ttk.Frame(frame)
    controls.pack(fill="x", pady=18)
    ttk.Label(controls, text="协议范围：").pack(side="left")
    protocol = tk.StringVar(value=args.protocol)
    selector = ttk.Combobox(controls, textvariable=protocol, values=["all"],
                            state="readonly", width=10)
    selector.pack(side="left", padx=(0, 12))
    estimate = tk.StringVar()
    ttk.Label(frame, textvariable=estimate, wraplength=800).pack(anchor="w", pady=(0, 10))
    progress = ttk.Progressbar(frame, maximum=100)
    progress.pack(fill="x")
    state = tk.StringVar(value="准备就绪：all 表示当前配置全部协议。")
    ttk.Label(frame, textvariable=state, wraplength=710).pack(anchor="w", pady=10)
    summary = tk.StringVar(value="完成后显示本轮最高综合分；报告包含所有节点的地区、排名、CSV 和 JSON。")
    ttk.Label(frame, textvariable=summary, wraplength=800).pack(anchor="w", pady=(0, 8))
    log = tk.Text(frame, height=14, wrap="word", font=("Microsoft YaHei", 9))
    log.pack(fill="both", expand=True)
    log.configure(state="disabled")

    def refresh(show_error=True):
        try:
            source, executable = resolve_paths(config_path.get().strip() or None,
                                                core_path.get().strip() or None)
            _, nodes = load_nodes(source)
            options, selected = protocol_options(nodes, protocol.get())
            reset = selected != protocol.get().lower()
            selector.configure(values=options)
            protocol.set(selected)
            estimate.set(f"当前配置：{len(nodes)} 个节点 · 最大下载约 {len(nodes)*2*args.download_mib} MiB")
            path_note.set(f"已识别配置：{source}\n已识别核心：{executable}")
            state.set("原协议已不在新配置中，已恢复 all（全部协议）。" if reset else "配置与协议列表已刷新。")
            return True
        except (OSError, ValueError, KeyError, yaml.YAMLError) as error:
            estimate.set("配置未就绪，请确认路径并在 FlClash 中启用订阅。")
            state.set(str(error))
            if show_error:
                messagebox.showerror("环境识别失败", str(error))
            return False

    def choose_path(variable, kind):
        filetypes = [("FlClash 配置", "*.yaml *.yml")] if kind == "config" else [("FlClash 程序", "*.exe")]
        selected = filedialog.askopenfilename(title="选择当前生效的 config.yaml" if kind == "config"
                                               else "选择 FlClash.exe 或 FlClashCore.exe", filetypes=filetypes)
        if selected:
            variable.set(selected)
            refresh(False)

    def automatic():
        config_path.set("")
        core_path.set("")
        refresh()

    def start():
        nonlocal running
        if running:
            return
        if not refresh():
            return
        running = True
        cancel.clear()
        start_button.configure(state="disabled")
        stop_button.configure(state="normal")
        selector.configure(state="disabled")
        for control in path_controls + [refresh_button, auto_button]:
            control.configure(state="disabled")
        progress["value"] = 0
        summary.set("测评中：每批重新读取当前配置，评分与报告按批次保存。")
        current = copy.copy(args)
        current.protocol = protocol.get()
        current.config = config_path.get().strip() or None
        current.core = core_path.get().strip() or None

        def worker():
            try:
                path = run(current, cancel,
                           notify=lambda text: messages.put(("log", text)),
                           progress=lambda value, total: messages.put(("progress", value / total * 100)))
                messages.put(("done", path))
            except Exception as error:
                messages.put(("error", f"{type(error).__name__}: {error}"))
        threading.Thread(target=worker, daemon=True).start()

    start_button = ttk.Button(controls, text="一键测评", command=start)
    start_button.pack(side="left", padx=4)
    stop_button = ttk.Button(controls, text="停止并保存", command=cancel.set, state="disabled")
    stop_button.pack(side="left", padx=4)
    ttk.Button(controls, text="打开报告", command=lambda: webbrowser.open(last_report.as_uri())
               if last_report else None).pack(side="left", padx=4)
    refresh_button = ttk.Button(controls, text="刷新配置", command=refresh)
    refresh_button.pack(side="left", padx=4)
    auto_button = ttk.Button(controls, text="自动识别", command=automatic)
    auto_button.pack(side="left", padx=4)

    def poll():
        nonlocal running, last_report
        try:
            while True:
                kind, value = messages.get_nowait()
                if kind == "log":
                    state.set(value)
                    log.configure(state="normal")
                    log.insert("end", value + "\n")
                    log.see("end")
                    log.configure(state="disabled")
                elif kind == "progress":
                    progress["value"] = value
                else:
                    running = False
                    start_button.configure(state="normal")
                    stop_button.configure(state="disabled")
                    selector.configure(state="readonly")
                    for control in path_controls + [refresh_button, auto_button]:
                        control.configure(state="normal")
                    if kind == "done":
                        last_report = value
                        state.set("已停止并保存。" if cancel.is_set() else "检测完成，报告已生成。")
                        try:
                            summary.set(completion_summary(value))
                        except (OSError, ValueError):
                            summary.set("报告已生成，请点击“打开报告”查看结果。")
                        if not cancel.is_set():
                            progress["value"] = 100
                    else:
                        state.set(value)
                        messagebox.showerror("检测未完成", value)
        except queue.Empty:
            pass
        root.after(150, poll)

    def close():
        if running:
            cancel.set()
            state.set("正在停止并释放独立核心和浏览器，请稍候……")
            root.after(250, finish_close)
        else:
            root.destroy()

    def finish_close():
        if running:
            root.after(250, finish_close)
        else:
            root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    refresh(False)
    poll()
    if args.autorun:
        root.after(400, start)
    root.mainloop()
