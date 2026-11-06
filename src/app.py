# -*- coding: utf-8 -*-
"""tkinter 图形界面：设置配对码与共享夹、发现/连接对端、查看同步日志。"""
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext, simpledialog

from . import config as cfgmod
from . import tray as traymod
from .engine import SyncEngine


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = cfgmod.load()
        self.log_q: queue.Queue = queue.Queue()
        self.tray_q: queue.Queue = queue.Queue()
        self.tray = traymod.TrayIcon("游戏存档同步 GameSaveSync", self.tray_q)
        self.engine = SyncEngine(self.cfg, self._enqueue_log)
        self._balloon_shown = False

        root.title("游戏存档同步 · GameSaveSync")
        root.geometry("780x700")
        root.minsize(720, 620)

        self._build_ui()
        self._refresh_shares()
        self._refresh_addresses()
        self._poll_log()
        self._poll_tray()
        self._poll_peers()
        # 关闭按钮 = 最小化到托盘（Windows）；其他平台直接退出
        if traymod.available():
            root.protocol("WM_DELETE_WINDOW", self._hide_to_tray)
            self.tray.start()
        else:
            root.protocol("WM_DELETE_WINDOW", self._quit_app)
        # 启动即开监听，便于被对端连接
        self.root.after(300, self.toggle_service)

    # ---------------- UI ----------------
    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # 顶部：服务开关
        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        self.btn_service = ttk.Button(top, text="停止服务", command=self.toggle_service)
        self.btn_service.pack(side="left")
        self.lbl_status = ttk.Label(top, text="启动中…", foreground="#1a7f37")
        self.lbl_status.pack(side="left", padx=12)

        # 设备与配对
        dev = ttk.LabelFrame(self.root, text="本机设置（两台电脑配对码必须一致）")
        dev.pack(fill="x", **pad)
        ttk.Label(dev, text="本机名称：").grid(row=0, column=0, sticky="e", padx=6, pady=4)
        self.ent_name = ttk.Entry(dev, width=18)
        self.ent_name.insert(0, self.cfg.device_name)
        self.ent_name.grid(row=0, column=1, padx=4)
        ttk.Label(dev, text="配对码：").grid(row=0, column=2, sticky="e", padx=6)
        self.ent_token = ttk.Entry(dev, width=18)
        self.ent_token.insert(0, self.cfg.token)
        self.ent_token.grid(row=0, column=3, padx=4)
        ttk.Button(dev, text="重新生成", command=self._regen_token).grid(row=0, column=4, padx=4)
        ttk.Label(dev, text="端口：").grid(row=0, column=5, sticky="e", padx=6)
        self.ent_port = ttk.Entry(dev, width=8)
        self.ent_port.insert(0, str(self.cfg.port))
        self.ent_port.grid(row=0, column=6, padx=4)
        ttk.Button(dev, text="保存设置", command=self._save_basic).grid(row=0, column=7, padx=8)

        ttk.Label(dev, text="本机地址（异地时把 IPv6 地址发给另一台电脑手动填写）：").grid(
            row=1, column=0, columnspan=4, sticky="w", padx=6)
        self.lst_addr = tk.Listbox(dev, height=2, activestyle="none")
        self.lst_addr.grid(row=2, column=0, columnspan=8, sticky="we", padx=6, pady=4)
        dev.columnconfigure(1, weight=1)

        # 共享夹
        share = ttk.LabelFrame(self.root, text="要同步的文件夹（两端按「名称」配对，名称必须完全一致）")
        share.pack(fill="both", expand=False, **pad)
        cols = ("name", "path")
        self.tree = ttk.Treeview(share, columns=cols, show="headings", height=4)
        self.tree.heading("name", text="共享夹名称")
        self.tree.heading("path", text="本地路径")
        self.tree.column("name", width=160, anchor="w")
        self.tree.column("path", width=520, anchor="w")
        self.tree.pack(fill="x", padx=6, pady=4)
        bar = ttk.Frame(share)
        bar.pack(fill="x", padx=6, pady=4)
        ttk.Button(bar, text="添加文件夹…", command=self._add_share).pack(side="left")
        ttk.Button(bar, text="删除选中", command=self._remove_share).pack(side="left", padx=6)

        # 对端连接
        peer = ttk.LabelFrame(self.root, text="对端电脑（局域网自动发现；异地填 IPv6）")
        peer.pack(fill="both", expand=False, **pad)
        self.lst_peers = tk.Listbox(peer, height=3, activestyle="none")
        self.lst_peers.pack(fill="x", padx=6, pady=4)
        prow = ttk.Frame(peer)
        prow.pack(fill="x", padx=6, pady=4)
        ttk.Label(prow, text="地址（IPv4 或 [IPv6]:端口）：").pack(side="left")
        self.ent_target = ttk.Entry(prow)
        self.ent_target.pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(prow, text="连接", command=self._connect_manual).pack(side="left")
        self.lst_peers.bind("<Double-Button-1>", lambda e: self._connect_selected())

        # 同步选项
        opt = ttk.LabelFrame(self.root, text="同步选项")
        opt.pack(fill="x", **pad)
        self.var_auto = tk.BooleanVar(value=self.cfg.auto_sync)
        ttk.Checkbutton(opt, text="自动同步（连接后按间隔周期比对）", variable=self.var_auto,
                        command=self._save_options).pack(side="left", padx=8)
        ttk.Label(opt, text="间隔（秒）：").pack(side="left")
        self.ent_interval = ttk.Entry(opt, width=6)
        self.ent_interval.insert(0, str(self.cfg.interval))
        self.ent_interval.pack(side="left")
        ttk.Button(opt, text="立即同步一次",
                   command=lambda: threading.Thread(target=self.engine.run_sync_once,
                                                    daemon=True).start()).pack(side="left", padx=12)
        self.var_delete = tk.BooleanVar(value=self.cfg.delete_mirror)
        ttk.Checkbutton(opt, text="删除镜像（对端删除也删，谨慎）", variable=self.var_delete,
                        command=self._save_options).pack(side="left", padx=8)
        ttk.Label(opt, text="（默认只新增/覆盖，不删除文件，避免误删存档）",
                  foreground="#888").pack(side="left", padx=8)

        # WebDAV 云盘备份
        wd = ttk.LabelFrame(self.root, text="WebDAV 云盘备份（Cloudreve / 坚果云 / Nextcloud 等）")
        wd.pack(fill="x", **pad)
        self.var_webdav = tk.BooleanVar(value=self.cfg.webdav_enabled)
        ttk.Checkbutton(wd, text="启用", variable=self.var_webdav).pack(side="left", padx=4)
        ttk.Label(wd, text="地址：").pack(side="left")
        self.ent_wd_url = ttk.Entry(wd, width=26)
        self.ent_wd_url.insert(0, self.cfg.webdav_url)
        self.ent_wd_url.pack(side="left")
        ttk.Label(wd, text="用户名：").pack(side="left")
        self.ent_wd_user = ttk.Entry(wd, width=14)
        self.ent_wd_user.insert(0, self.cfg.webdav_user)
        self.ent_wd_user.pack(side="left")
        ttk.Label(wd, text="密码：").pack(side="left")
        self.ent_wd_pass = ttk.Entry(wd, width=14, show="*")
        self.ent_wd_pass.insert(0, self.cfg.webdav_pass)
        self.ent_wd_pass.pack(side="left")
        ttk.Button(wd, text="保存", command=self._save_webdav).pack(side="left", padx=6)
        ttk.Button(wd, text="立即备份", command=self._webdav_backup_now).pack(side="left")
        ttk.Label(wd, text="（备份到 <地址>/GameSaveSync/<共享夹名>，随同步周期自动进行；"
                           "Cloudreve 需开启 WebDAV）",
                  foreground="#888").pack(side="left", padx=6)

        # 日志
        logf = ttk.LabelFrame(self.root, text="日志")
        logf.pack(fill="both", expand=True, **pad)
        self.txt_log = scrolledtext.ScrolledText(logf, height=10, state="disabled", font=("Consolas", 9))
        self.txt_log.pack(fill="both", expand=True, padx=6, pady=4)

    # ---------------- 事件 ----------------
    def toggle_service(self):
        if self.engine.running:
            self.engine.stop()
            self.btn_service.config(text="启动服务")
            self.lbl_status.config(text="已停止", foreground="#b3261e")
        else:
            try:
                self._save_basic(silent=True)
                self.engine.start()
                self.btn_service.config(text="停止服务")
                self.lbl_status.config(text="监听中", foreground="#1a7f37")
            except OSError as e:
                messagebox.showerror("启动失败", f"端口 {self.cfg.port} 无法绑定：\n{e}\n可换一个端口再试。")

    def _regen_token(self):
        self.ent_token.delete(0, "end")
        self.ent_token.insert(0, cfgmod.generate_token())
        self._save_basic(silent=True)

    def _save_basic(self, silent=False):
        try:
            self.cfg.device_name = self.ent_name.get()
            self.cfg.token = self.ent_token.get()
            self.cfg.port = int(self.ent_port.get())
            self.cfg.save()
            if not silent:
                messagebox.showinfo("已保存", "本机设置已保存。")
        except ValueError:
            messagebox.showerror("错误", "端口必须是 1024-65535 的数字。")

    def _save_options(self):
        self.cfg.auto_sync = self.var_auto.get()
        self.cfg.delete_mirror = self.var_delete.get()
        try:
            self.cfg.interval = int(self.ent_interval.get())
        except ValueError:
            pass
        self.cfg.save()

    def _save_webdav(self):
        self.cfg.webdav_enabled = self.var_webdav.get()
        self.cfg.webdav_url = self.ent_wd_url.get()
        self.cfg.webdav_user = self.ent_wd_user.get()
        self.cfg.webdav_pass = self.ent_wd_pass.get()
        self.cfg.save()
        messagebox.showinfo("已保存", "WebDAV 设置已保存。\n"
                            "若服务正在运行，备份将按同步间隔自动进行；"
                            "也可点「立即备份」马上上传一次。")

    def _webdav_backup_now(self):
        # 先把当前输入保存下来再备份，避免「改了没保存就备份」的困惑
        self.cfg.webdav_enabled = self.var_webdav.get()
        self.cfg.webdav_url = self.ent_wd_url.get()
        self.cfg.webdav_user = self.ent_wd_user.get()
        self.cfg.webdav_pass = self.ent_wd_pass.get()
        self.cfg.save()
        threading.Thread(target=self.engine.webdav.run_once,
                         kwargs={"manual": True}, daemon=True).start()

    def _refresh_addresses(self):
        self.lst_addr.delete(0, "end")
        for ip in cfgmod.local_addresses():
            self.lst_addr.insert("end", ip)

    def _refresh_shares(self):
        for i in self.tree.get_children():
            self.tree.delete(i)
        for s in self.cfg.shares:
            self.tree.insert("", "end", values=(s["name"], s["path"]))

    def _add_share(self):
        name = simpledialog.askstring("共享夹名称", "给这个存档文件夹起个名字（两端要一致）：", parent=self.root)
        if not name:
            return
        path = filedialog.askdirectory(title=f"选择「{name}」对应的本地存档文件夹", parent=self.root)
        if not path:
            return
        self.cfg.add_share(name.strip(), path)
        self.cfg.save()
        self._refresh_shares()

    def _remove_share(self):
        sel = self.tree.selection()
        if not sel:
            return
        name = self.tree.item(sel[0], "values")[0]
        self.cfg.remove_share(name)
        self.cfg.save()
        self._refresh_shares()

    def _connect_manual(self):
        target = self.ent_target.get().strip()
        if not target:
            return
        # 连接含握手 + 首轮同步，放后台线程避免卡界面
        threading.Thread(target=self.engine.connect, args=(target,), daemon=True).start()

    def _connect_selected(self):
        sel = self.lst_peers.curselection()
        if not sel:
            return
        item = self.lst_peers.get(sel[0])
        # 列表项格式：名称  addr:port
        addr = item.split()[-1]
        threading.Thread(target=self.engine.connect, args=(addr,), daemon=True).start()

    # ---------------- 托盘与退出 ----------------
    def _hide_to_tray(self):
        self.root.withdraw()
        if not self._balloon_shown:
            self._balloon_shown = True
            self.tray.show_balloon("游戏存档同步",
                                   "程序已最小化到系统托盘，同步仍在后台进行。\n"
                                   "单击托盘图标恢复窗口，右键可退出。")

    def _poll_tray(self):
        while True:
            try:
                evt = self.tray_q.get_nowait()
            except queue.Empty:
                break
            if evt == traymod.EVT_RESTORE:
                self.root.deiconify()
                self.root.lift()
                self.root.focus_force()
            elif evt == traymod.EVT_QUIT:
                self._quit_app()
                return
        self.root.after(300, self._poll_tray)

    def _quit_app(self):
        self.engine.stop()
        self.cfg.save()
        self.tray.stop()
        self.root.destroy()

    # ---------------- 轮询 ----------------
    def _enqueue_log(self, msg: str):
        self.log_q.put(msg)

    def _poll_log(self):
        while True:
            try:
                msg = self.log_q.get_nowait()
            except queue.Empty:
                break
            self.txt_log.config(state="normal")
            self.txt_log.insert("end", msg + "\n")
            self.txt_log.see("end")
            self.txt_log.config(state="disabled")
        self.root.after(300, self._poll_log)

    def _poll_peers(self):
        self.lst_peers.delete(0, "end")
        for p in self.engine.discovery.snapshot():
            tag = "v6" if p["v6"] else "v4"
            self.lst_peers.insert("end", f"{p['name']}  [{tag}]  {p['addr']}:{p['port']}")
        self.root.after(3000, self._poll_peers)


def main():
    root = tk.Tk()
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
